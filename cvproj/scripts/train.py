import os
import gc
import lpips
import clip
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.utils import set_seed
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
import vision_aided_loss

import diffusers
from diffusers.optimization import get_scheduler

import wandb
from cleanfid.fid import get_folder_features, build_feature_extractor, fid_from_feats

from cvproj.models.pix2pix import Pix2Pix_Turbo
from cvproj.data.dataset import PairedDataset, PokemonDataset, PixelDataset, SketchyDataset
from cvproj.data.configs import TrainConfig


def main(cfg: TrainConfig):
    accelerator = Accelerator(
        log_with=cfg.logger_type,
        gradient_accumulation_steps=20,
    )

    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    if cfg.seed is not None:
        set_seed(cfg.seed)

    if accelerator.is_main_process:
        os.makedirs(os.path.join(cfg.output_dir, "checkpoints"), exist_ok=True)
        os.makedirs(os.path.join(cfg.output_dir, "eval"), exist_ok=True)

    net_pix2pix = Pix2Pix_Turbo(
        pretrained_path=cfg.pretrained_path,
        lora_rank_unet=cfg.lora_rank_unet,
        lora_rank_vae=cfg.lora_rank_vae,
        device=cfg.device,
        diff_steps=cfg.diff_steps
    )

    net_pix2pix.set_train()
    net_pix2pix.unet.enable_xformers_memory_efficient_attention()
    net_pix2pix.unet.enable_gradient_checkpointing()
    torch.backends.cuda.matmul.allow_tf32 = True

    net_disc = vision_aided_loss.Discriminator(
        cv_type="clip", loss_type="multilevel_sigmoid_s", device=cfg.device
    )

    net_disc = net_disc.to(cfg.device)
    net_disc.requires_grad_(True)
    net_disc.cv_ensemble.requires_grad_(False)
    net_disc.train()

    net_lpips = lpips.LPIPS(net="vgg").to(cfg.device)
    net_lpips.requires_grad_(False)

    net_clip, _ = clip.load("ViT-B/32", device=cfg.device)
    net_clip.requires_grad_(False)
    net_clip.eval()

    # make the optimizer
    layers_to_opt = []
    for n, _p in net_pix2pix.unet.named_parameters():
        if "lora" in n:
            assert _p.requires_grad
            layers_to_opt.append(_p)
    layers_to_opt += list(net_pix2pix.unet.conv_in.parameters())
    for n, _p in net_pix2pix.vae.named_parameters():
        if "lora" in n and "vae_skip" in n:
            assert _p.requires_grad
            layers_to_opt.append(_p)
    layers_to_opt = (
        layers_to_opt
        + list(net_pix2pix.vae.decoder.skip_conv_1.parameters())
        + list(net_pix2pix.vae.decoder.skip_conv_2.parameters())
        + list(net_pix2pix.vae.decoder.skip_conv_3.parameters())
        + list(net_pix2pix.vae.decoder.skip_conv_4.parameters())
    )

    optimizer = torch.optim.AdamW(layers_to_opt, lr=cfg.learning_rate)

    lr_scheduler = get_scheduler(
        cfg.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=cfg.lr_warmup_steps * accelerator.num_processes,
        num_cycles=cfg.lr_num_cycles,
        power=cfg.lr_power,
    )

    optimizer_disc = torch.optim.AdamW(
        net_disc.parameters(),
        lr=cfg.learning_rate,
    )
    lr_scheduler_disc = get_scheduler(
        cfg.lr_scheduler,
        optimizer=optimizer_disc,
        num_warmup_steps=cfg.lr_warmup_steps * accelerator.num_processes,
        num_cycles=cfg.lr_num_cycles,
        power=cfg.lr_power,
    )

    if cfg.dataset_type == "sketchy":
        dataset_train = SketchyDataset(
            split="train",
            dataset_folder=cfg.dataset_folder,
            tokenizer=net_pix2pix.tokenizer,
        )
        dataset_val = SketchyDataset(
            split="val",
            dataset_folder=cfg.dataset_folder,
            tokenizer=net_pix2pix.tokenizer,
        )
    elif cfg.dataset_type == "pokemon":
        dataset_train = PokemonDataset(
            split="train",
            tokenizer=net_pix2pix.tokenizer,
        )
        dataset_val = PokemonDataset(
            split="train",
            tokenizer=net_pix2pix.tokenizer,
        )
    elif cfg.dataset_type == "pixel":
        dataset_train = PixelDataset(
            split="train",
            tokenizer=net_pix2pix.tokenizer,
        )
        dataset_val = PixelDataset(
            split="train",
            tokenizer=net_pix2pix.tokenizer,
        )

    elif cfg.dataset_type == "paired":
        dataset_train = PairedDataset(
            dataset_folder=cfg.dataset_folder,
            split="train",
            tokenizer=net_pix2pix.tokenizer,
        )
        # dataset_val = PairedDataset(
        #     dataset_folder=cfg.dataset_folder,
        #     split="test",
        #     tokenizer=net_pix2pix.tokenizer,
        # )
    else:
        raise ValueError(f"Unknown dataset type '{cfg.dataset_type}'")

    dl_train = torch.utils.data.DataLoader(
        dataset_train,
        batch_size=cfg.train_batch_size,
        shuffle=True,
        num_workers=cfg.train_dataloader_num_workers,
    )

    dl_val = torch.utils.data.DataLoader(
        dataset_val, batch_size=cfg.eval_batch_size, shuffle=False, num_workers=0
    )

    (
        net_pix2pix,
        net_disc,
        optimizer,
        optimizer_disc,
        dl_train,
        lr_scheduler,
        lr_scheduler_disc,
    ) = accelerator.prepare(
        net_pix2pix,
        net_disc,
        optimizer,
        optimizer_disc,
        dl_train,
        lr_scheduler,
        lr_scheduler_disc,
    )
    net_clip, net_lpips = accelerator.prepare(net_clip, net_lpips)

    # renorm with image net statistics
    t_clip_renorm = transforms.Normalize(
        mean=(0.48145466, 0.4578275, 0.40821073),
        std=(0.26862954, 0.26130258, 0.27577711),
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move al networksr to device and cast to weight_dtype
    net_pix2pix.to(accelerator.device, dtype=weight_dtype)
    net_disc.to(accelerator.device, dtype=weight_dtype)
    net_lpips.to(accelerator.device, dtype=weight_dtype)
    net_clip.to(accelerator.device, dtype=weight_dtype)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        tracker_config = dict(vars(cfg))
        accelerator.init_trackers(
            cfg.tracker_project_name, config=tracker_config)

    max_train_steps = cfg.epoch_num * \
        len(dataset_train) // cfg.train_batch_size
    progress_bar = tqdm(
        range(0, max_train_steps),
        initial=0,
        desc="Steps",
        disable=not accelerator.is_local_main_process,
    )

    # turn off eff. attn for the discriminator
    for name, module in net_disc.named_modules():
        if "attn" in name:
            module.fused_attn = False

    # compute the reference stats for FID tracking
    if accelerator.is_main_process and cfg.track_fid_metrci_val:
        feat_model = build_feature_extractor(
            "clean", cfg.device, use_dataparallel=False
        )

        def fn_transform(x):
            x_pil = Image.fromarray(x)
            out_pil = transforms.Resize(
                cfg.resolution, interpolation=transforms.InterpolationMode.LANCZOS
            )(x_pil)
            return np.array(out_pil)

        ref_stats = get_folder_features(
            os.path.join(cfg.dataset_folder, "val"),
            model=feat_model,
            num_workers=0,
            num=None,
            shuffle=False,
            seed=0,
            batch_size=8,
            # device=torch.to(cfg.device),
            device=torch.device(cfg.device),
            mode="clean",
            custom_image_tranform=fn_transform,
            description="",
            verbose=True,
        )

    # start the training loop
    global_step = 0
    for epoch in range(0, cfg.epoch_num):
        for step, batch in enumerate(dl_train):
            l_acc = [net_pix2pix, net_disc]
            with accelerator.accumulate(*l_acc):
                x_src = batch["conditioning_pixel_values"]
                x_tgt = batch["output_pixel_values"]
                B, C, H, W = x_src.shape
                # forward pass
                x_tgt_pred = net_pix2pix(
                    x_src, prompt_tokens=batch["input_ids"], deterministic=True
                )
                # Reconstruction loss
                loss_l2 = (
                    F.mse_loss(x_tgt_pred.float(),
                               x_tgt.float(), reduction="mean")
                    * cfg.l_rec
                )
                loss_lpips = (
                    net_lpips(x_tgt_pred.float(), x_tgt.float()
                              ).mean() * cfg.l_lpips
                )
                # loss_l2 = torch.tensor(-1)
                # loss_lpips = torch.tensor(-1)

                loss = loss_l2 + loss_lpips
                # CLIP similarity loss
                if cfg.l_clipsim > 0:
                    x_tgt_pred_renorm = t_clip_renorm(x_tgt_pred * 0.5 + 0.5)
                    x_tgt_pred_renorm = F.interpolate(
                        x_tgt_pred_renorm,
                        (224, 224),
                        mode="bilinear",
                        align_corners=False,
                    )
                    caption_tokens = clip.tokenize(batch["caption"], truncate=True).to(
                        x_tgt_pred.device
                    )
                    clipsim, _ = net_clip(x_tgt_pred_renorm, caption_tokens)
                    loss_clipsim = 1 - clipsim.mean() / 100
                    loss = loss_clipsim * cfg.l_clipsim

                accelerator.backward(loss, retain_graph=False)

                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(layers_to_opt, cfg.grad_clip)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=True)

                """
                Generator loss: fool the discriminator
                """
                x_tgt_pred = net_pix2pix(
                    x_src, prompt_tokens=batch["input_ids"], deterministic=True
                )
                lossG = net_disc(
                    x_tgt_pred, for_G=True).mean() * cfg.l_gan
                accelerator.backward(lossG)

                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(
                        layers_to_opt, cfg.grad_clip)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=True)

                """
                Discriminator loss: fake image vs real image
                """
                # real image
                lossD_real = (
                    net_disc(x_tgt.detach(), for_real=True).mean() *
                    cfg.l_gan
                )
                accelerator.backward(lossD_real.mean())

                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(
                        net_disc.parameters(), cfg.grad_clip
                    )
                optimizer_disc.step()
                lr_scheduler_disc.step()
                optimizer_disc.zero_grad(set_to_none=True)

                # fake image
                lossD_fake = (
                    net_disc(x_tgt_pred.detach(), for_real=False).mean()
                    * cfg.l_gan
                )
                accelerator.backward(lossD_fake.mean())
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(
                        net_disc.parameters(), cfg.grad_clip
                    )

                optimizer_disc.step()
                optimizer_disc.zero_grad(set_to_none=True)
                lossD = lossD_real + lossD_fake

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1

                if accelerator.is_main_process:
                    logs = {}
                    # log all the losses
                    logs["lossG"] = lossG.detach().item()
                    logs["lossD"] = lossD.detach().item()
                    logs["loss_l2"] = loss_l2.detach().item()
                    logs["loss_lpips"] = loss_lpips.detach().item()
                    if cfg.l_clipsim > 0:
                        logs["loss_clipsim"] = loss_clipsim.detach().item()
                    progress_bar.set_postfix(**logs)

                    # viz some images
                    if global_step % cfg.image_log_freq == 1:
                        log_dict = {
                            "train/source": [
                                wandb.Image(
                                    x_src[idx].float().detach().cpu(),
                                    caption=f"{batch['caption'][idx]}",
                                )
                                for idx in range(B)
                            ],
                            "train/target": [
                                wandb.Image(
                                    x_tgt[idx].float().detach().cpu(),
                                    caption=f"idx={idx}",
                                )
                                for idx in range(B)
                            ],
                            "train/model_output": [
                                wandb.Image(
                                    x_tgt_pred[idx].float().detach().cpu(),
                                    caption=f"idx={idx}",
                                )
                                for idx in range(B)
                            ],
                        }
                        for k in log_dict:
                            logs[k] = log_dict[k]

                    # checkpoint the model
                    if global_step % cfg.model_log_freq == 1:
                        outf = os.path.join(
                            cfg.output_dir, "checkpoints", f"model_{global_step}.pkl"
                        )
                        accelerator.unwrap_model(net_pix2pix).save_model(outf)

                    # compute validation set FID, L2, LPIPS, CLIP-SIM
                    if global_step % cfg.eval_freq == 1:
                        l_l2, l_lpips, l_clipsim = [], [], []
                        if cfg.track_fid_metrci_val:
                            os.makedirs(
                                os.path.join(
                                    cfg.output_dir, "eval", f"fid_{global_step}"
                                ),
                                exist_ok=True,
                            )
                        for step, batch_val in enumerate(dl_val):
                            if step >= cfg.num_samples_to_eval:
                                break
                            x_src = batch_val["conditioning_pixel_values"].to(
                                cfg.device
                            )
                            x_tgt = batch_val["output_pixel_values"].to(
                                cfg.device)
                            B, C, H, W = x_src.shape
                            assert B == 1, "Use batch size 1 for eval."
                            with torch.no_grad():
                                # forward pass
                                x_tgt_pred = accelerator.unwrap_model(net_pix2pix)(
                                    x_src,
                                    prompt_tokens=batch_val["input_ids"].to(
                                        cfg.device),
                                    deterministic=True,
                                )
                                # compute the reconstruction losses
                                loss_l2 = F.mse_loss(
                                    x_tgt_pred.float(), x_tgt.float(), reduction="mean"
                                )
                                loss_lpips = net_lpips(
                                    x_tgt_pred.float(), x_tgt.float()
                                ).mean()
                                # compute clip similarity loss
                                x_tgt_pred_renorm = t_clip_renorm(
                                    x_tgt_pred * 0.5 + 0.5
                                )
                                x_tgt_pred_renorm = F.interpolate(
                                    x_tgt_pred_renorm,
                                    (224, 224),
                                    mode="bilinear",
                                    align_corners=False,
                                )
                                caption_tokens = clip.tokenize(
                                    batch_val["caption"], truncate=True
                                ).to(x_tgt_pred.device)
                                clipsim, _ = net_clip(
                                    x_tgt_pred_renorm, caption_tokens)
                                clipsim = clipsim.mean()

                                l_l2.append(loss_l2.item())
                                l_lpips.append(loss_lpips.item())
                                l_clipsim.append(clipsim.item())

                            # save output images to file for FID evaluation
                            if cfg.track_fid_metrci_val:
                                output_pil = transforms.ToPILImage()(
                                    x_tgt_pred[0].cpu() * 0.5 + 0.5
                                )
                                outf = os.path.join(
                                    cfg.output_dir,
                                    "eval",
                                    f"fid_{global_step}",
                                    f"val_{step}.png",
                                )
                                output_pil.save(outf)
                        if cfg.track_fid_metrci_val:
                            curr_stats = get_folder_features(
                                os.path.join(
                                    cfg.output_dir, "eval", f"fid_{global_step}"
                                ),
                                model=feat_model,
                                num_workers=0,
                                num=None,
                                shuffle=False,
                                seed=0,
                                batch_size=8,
                                device=torch.device(cfg.device),
                                mode="clean",
                                custom_image_tranform=fn_transform,
                                description="",
                                verbose=True,
                            )
                            fid_score = fid_from_feats(ref_stats, curr_stats)
                            logs["val/clean_fid"] = fid_score
                        logs["val/l2"] = np.mean(l_l2)
                        logs["val/lpips"] = np.mean(l_lpips)
                        logs["val/clipsim"] = np.mean(l_clipsim)
                        gc.collect()
                        torch.cuda.empty_cache()
                    accelerator.log(logs, step=global_step)


if __name__ == "__main__":
    cfg = TrainConfig(
        device="cuda",
        train_batch_size=4,
        learning_rate=1e-5,
        grad_clip=1,
        dataset_type="sketchy",
        dataset_folder="/home/patratskiy_ma/study/CVProj/data/SketchyCaptions",
        epoch_num=5,
        # pretrained_path="/home/patratskiy_ma/study/CVProj/log_finetune/checkpoints/model_1501.pkl",
        output_dir="exp/sketchy_v2"
    )
    main(cfg)
    # print(os.environ["CUDA_VISIBLE_DEVICES"])
    # for i in range(torch.cuda.device_count()):
    #     print(torch.cuda.get_device_properties(i).name)
