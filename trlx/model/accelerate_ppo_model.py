import os
from abc import abstractmethod
from typing import Dict, Iterable, Tuple

import torch
import torch.nn.functional as F
from accelerate import Accelerator
from torch.utils.data import DataLoader
from torchtyping import TensorType
from tqdm import tqdm
from transformers import AutoConfig, AutoTokenizer
import numpy as np

import wandb
from trlx.data.accelerate_base_datatypes import PromptBatch
from trlx.data.configs import TRLConfig
from trlx.model import BaseRLModel, register_model
from trlx.model.accelerate_base_model import AccelerateRLModel
from trlx.model.nn.ppo_models import GPTHeadWithValueModel, GPTHydraHeadWithValueModel
from trlx.pipeline.ppo_pipeline import PPORolloutStorage
from trlx.utils import Clock, rampup_decay, safe_mkdir, topk_mask
from trlx.utils.modeling import clip_by_value, logprobs_from_logits, whiten

class AdaptiveKLController:
    def __init__(self, init_kl_coef, target, horizon):
        self.value = init_kl_coef
        self.target = target
        self.horizon = horizon

    def update(self, current, n_steps):
        target = self.target
        proportional_error = np.clip(current / target - 1, -0.2, 0.2)
        mult = 1 + proportional_error * n_steps / self.horizon
        self.value *= mult

# Cell

class FixedKLController:
    """Fixed KL controller."""
    def __init__(self, kl_coef):
        self.value = kl_coef

    def update(self, current, n_steps):
        pass

@register_model
class AcceleratePPOModel(AccelerateRLModel):
    def __init__(self, config, train_mode=True):
        self.store = PPORolloutStorage()
        super().__init__(config, self.store)

        if config.method.target is not None:
            self.kl_ctl = AdaptiveKLController(
                                                config.method.init_kl_coef, 
                                                config.method.target, 
                                                config.method.horizon
                                              )
        else:
            self.kl_ctl = FixedKLController(config.method.init_kl_coef)

    def get_arch(self, config: TRLConfig):
        # TODO(dahoas): Assumes model is gpt like
        return GPTHydraHeadWithValueModel(self.config.model.model_path, self.config.model.num_layers_unfrozen)

    def loss(
        self, query_tensors, response_tensors, all_logprobs, all_values, all_rewards
    ):
        lastgaelam = 0
        advantages_reversed = []
        gen_len = response_tensors.shape[1]
        for t in reversed(range(gen_len)):
            nextvalues = all_values[:, t + 1] if t < gen_len - 1 else 0.0
            delta = (
                all_rewards[:, t]
                + self.config.method.gamma * nextvalues
                - all_values[:, t]
            )
            lastgaelam = (
                delta + self.config.method.gamma * self.config.method.lam * lastgaelam
            )
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1]).transpose(0, 1)

        returns = advantages + all_values
        advantages = whiten(advantages)
        advantages = advantages.detach()

        all_tokens = torch.cat((query_tensors, response_tensors), dim=1)
        logits, _, vpred = self.model(all_tokens)
        logprob = logprobs_from_logits(logits[:, :-1, :], all_tokens[:, 1:])

        # only the generation part of the values/logprobs is needed
        logprob, vpred = logprob[:, -gen_len:], vpred[:, -gen_len - 1 : -1]

        vpredclipped = clip_by_value(
            vpred,
            all_values - self.config.method.cliprange_value,
            all_values + self.config.method.cliprange_value,
        )

        vf_losses1 = (vpred - returns) ** 2
        vf_losses2 = (vpredclipped - returns) ** 2
        vf_loss = 0.5 * torch.mean(torch.max(vf_losses1, vf_losses2))

        kl = logprob - all_logprobs
        # Record mean_kl for kl coef adjustment
        self.mean_kl = torch.mean(torch.sum(kl, dim=-1)).item()
        ratio = torch.exp(kl)

        pg_losses = -advantages * ratio
        pg_losses2 = -advantages * torch.clamp(
            ratio,
            1.0 - self.config.method.cliprange,
            1.0 + self.config.method.cliprange,
        )

        pg_loss = torch.mean(torch.max(pg_losses, pg_losses2))

        model_loss = pg_loss + self.config.method.vf_coef * vf_loss
        return model_loss, pg_loss, vf_loss

    def post_epoch_callback(self):
        # TODO(dahoas): are experiences being made for dataloaders on each process or same dataloader
        self.epoch += 1
        self.store.clear_history()
        self.orch.make_experience(
            self.config.method.num_rollouts, self.iter_count
        )  # Collect more rollouts for training

    def post_backward_callback(self):
        batch = self.logs["batch"]
        # Update kl_coefficient
        self.kl_ctl.update(self.mean_kl ,self.config.train.batch_size)
        # Run evaluation
        if self.accelerator.is_main_process:
            if (
                self.iter_count % self.config.train.eval_interval == 0
                or self.iter_count <= self.config.method.ppo_epochs
            ):
                text = self.tokenizer.batch_decode(batch.query_tensors)
                eval_batch: PromptBatch = PromptBatch(
                    text=text, tokens=batch.query_tensors
                )
                query_tensors, response_tensors, response_text = self.act(eval_batch)
                gen_texts = [q + r for q, r in zip(eval_batch.text, response_text)]
                scores = self.orch.score(gen_texts)
                mean_score = torch.mean(scores).item()
                rows = list(zip(gen_texts, scores.tolist()))
                stats = {
                    "mean_score": mean_score,
                    "responses": wandb.Table(columns=["response", "score"], rows=rows),
                    "pg_loss": self.logs["pg_loss"],
                    "vf_loss": self.logs["vf_loss"],
                    "kl_coef": self.kl_ctl.value,
                }
                self.accelerator.log(stats, step=self.iter_count)
                self.accelerator.print(
                    "Step: {}, Mean score: {}, pg_loss: {}, vf_loss: {}, kl_coef: {}".format(
                        self.iter_count, mean_score, stats["pg_loss"], stats["vf_loss"], self.kl_ctl.value,
                    )
                )

    def learn(self, log_fn=None, save_fn=None, eval_fn=None):

        self.accelerator.print("STARTING LEARNING")

        rollout_loader = self.store.create_loader(
            self.config.train.batch_size, shuffle=True, prep_fn=None, num_workers=2
        )
        rollout_loader = self.accelerator.prepare(rollout_loader)

        self.iter_count = 0
        self.epoch = 0
        while (
            self.iter_count < self.config.train.total_steps
            or self.epoch <= self.config.train.epochs
        ):
            for batch in rollout_loader:

                query_tensors = batch.query_tensors.to(self.accelerator.device)
                response_tensors = batch.response_tensors.to(self.accelerator.device)
                logprobs = batch.logprobs.to(self.accelerator.device)
                values = batch.values.to(self.accelerator.device)
                rewards = batch.rewards.to(self.accelerator.device)

                for _ in range(self.config.method.ppo_epochs):
                    loss, pg_loss, vf_loss = self.loss(
                        query_tensors, response_tensors, logprobs, values, rewards
                    )
                    self.logs = {
                        "loss": loss,
                        "pg_loss": pg_loss,
                        "vf_loss": vf_loss,
                        "batch": batch,
                        "rewards": rewards,
                    }
                    # self.post_backward_callback()
                    # exit()
                    self.opt.zero_grad()
                    self.accelerator.backward(loss)
                    self.opt.step()
                    self.scheduler.step()
                    self.iter_count += 1

                self.post_backward_callback()
                self.accelerator.wait_for_everyone()

            self.post_epoch_callback()
            self.accelerator.wait_for_everyone()
