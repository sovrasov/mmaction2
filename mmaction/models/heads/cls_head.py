import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from mmcv.cnn import constant_init, kaiming_init

from .base import BaseHead
from ..registry import HEADS
from ...core.ops import conv_1x1x1_bn, normalize, AngleMultipleLinear


@HEADS.register_module()
class ClsHead(BaseHead):
    def __init__(self,
                 spatial_type=None,
                 temporal_size=1,
                 spatial_size=7,
                 init_std=0.01,
                 embedding=False,
                 embd_size=128,
                 num_centers=1,
                 st_scale=5.0,
                 reg_threshold=0.1,
                 enable_sampling=False,
                 adaptive_sampling=False,
                 sampling_angle_std=None,
                 reg_weight=1.0,
                 enable_class_mixing=False,
                 class_mixing_alpha=0.1,
                 **kwargs):
        super(ClsHead, self).__init__(**kwargs)

        self.embd_size = embd_size
        self.temporal_feature_size = temporal_size
        self.spatial_feature_size = \
            spatial_size \
            if not isinstance(spatial_size, int) \
            else (spatial_size, spatial_size)
        self.init_std = init_std

        self.avg_pool = None
        if spatial_type == 'avg':
            self.avg_pool = nn.AdaptiveAvgPool3d((1, 1, 1))

        self.with_embedding = embedding and self.embd_size > 0
        if self.with_embedding:
            self.fc_pre_angular = None
            if self.in_channels != self.embd_size:
                self.fc_pre_angular = conv_1x1x1_bn(self.in_channels, self.embd_size, as_list=False)

            self.fc_angular = AngleMultipleLinear(self.embd_size, self.num_classes,
                                                  num_centers, st_scale,
                                                  reg_weight, reg_threshold)
        else:
            self.fc_cls_out = nn.Linear(self.in_channels, self.num_classes)

        self.enable_sampling = (self.with_embedding and
                                enable_sampling and
                                sampling_angle_std is not None and
                                sampling_angle_std > 0.0)
        self.adaptive_sampling = (self.enable_sampling and
                                  adaptive_sampling and
                                  self.class_sizes is not None)
        if self.enable_sampling:
            assert sampling_angle_std < 0.5 * np.pi

            if self.adaptive_sampling:
                counts = np.ones([self.num_classes], dtype=np.float32)
                for class_id, class_size in self.class_sizes.items():
                    counts[class_id] = class_size

                class_angle_std = sampling_angle_std * np.power(counts, -1. / 4.)
                self.register_buffer('sampling_angle_std', torch.from_numpy(class_angle_std))
            else:
                self.sampling_angle_std = sampling_angle_std

        self.enable_class_mixing = enable_class_mixing
        self.alpha_class_mixing = class_mixing_alpha

    def init_weights(self):
        if self.with_embedding:
            for m in self.modules():
                if isinstance(m, nn.Conv2d) or isinstance(m, nn.Conv3d):
                    kaiming_init(m)
                elif isinstance(m, nn.BatchNorm2d) or isinstance(m, nn.BatchNorm3d):
                    constant_init(m, 1.0, 0.0)
        else:
            nn.init.normal_(self.fc_cls_out.weight, 0, self.init_std)
            nn.init.constant_(self.fc_cls_out.bias, 0)

    def _squash_features(self, x):
        if x.ndimension() == 4:
            x = x.unsqueeze(2)

        if self.avg_pool is not None:
            x = self.avg_pool(x)

        return x

    @staticmethod
    def _mix_embd(norm_embd, labels, norm_centers, num_classes, alpha_class_mixing):
        with torch.no_grad():
            sampled_ids = torch.randint_like(labels, 0, num_classes - 1)
            sampled_neg_ids = torch.where(sampled_ids < labels, sampled_ids, sampled_ids + 1)
            random_centers = norm_centers[sampled_neg_ids]

        alpha = alpha_class_mixing * torch.rand_like(labels, dtype=norm_embd.dtype)
        mixed_embd = (1.0 - alpha.view(-1, 1)) * norm_embd + alpha.view(-1, 1) * random_centers
        norm_embd = normalize(mixed_embd, dim=1)

        return norm_embd

    @staticmethod
    def _sample_embd(norm_embd, labels, batch_size, adaptive_sampling, sampling_angle_std):
        with torch.no_grad():
            unit_directions = F.normalize(torch.randn_like(norm_embd), dim=1)
            dot_prod = torch.sum(norm_embd * unit_directions, dim=1, keepdim=True)
            orthogonal_directions = unit_directions - dot_prod * norm_embd

            if adaptive_sampling and labels is not None:
                all_angle_std = sampling_angle_std.expand(batch_size, -1)
                class_indices = torch.arange(batch_size, device=labels.device)
                angle_std = all_angle_std[class_indices, labels].view(-1, 1)
            else:
                angle_std = sampling_angle_std

            angles = angle_std * torch.randn_like(dot_prod)
            alpha = torch.clamp_max(torch.where(angles > 0.0, angles, torch.neg(angles)), 0.5 * np.pi)
            cos_alpha = torch.cos(alpha)
            sin_alpha = torch.sin(alpha)

        out_norm_embd = cos_alpha * norm_embd + sin_alpha * orthogonal_directions

        return out_norm_embd

    def forward(self, x, labels=None, return_extra_data=False, **kwargs):
        x = self._squash_features(x)

        if self.dropout is not None:
            x = self.dropout(x)

        if self.with_embedding:
            unnorm_embd = self.fc_pre_angular(x) if self.fc_pre_angular is not None else x
            norm_embd = normalize(unnorm_embd.view(-1, self.embd_size), dim=1)

            if self.training:
                if self.enable_class_mixing:
                    norm_class_centers = normalize(self.fc_angular.weight.permute(1, 0), dim=1)
                    norm_embd = self._mix_embd(
                        norm_embd, labels, norm_class_centers, self.num_classes, self.alpha_class_mixing
                    )

                if self.enable_sampling:
                    norm_embd = self._sample_embd(
                        norm_embd, labels, x.shape[0], self.adaptive_sampling, self.sampling_angle_std
                    )

            cls_score = self.fc_angular(norm_embd)
        else:
            norm_embd = None
            cls_score = self.fc_cls_out(x.view(-1, self.in_channels))

        if return_extra_data:
            return cls_score, norm_embd
        else:
            return cls_score

    def loss(self, cls_score, labels, norm_embd, name, **kwargs):
        losses = dict()

        losses['loss/cls' + name] = self.head_loss(cls_score, labels)
        if hasattr(self.head_loss, 'last_scale'):
            losses['scale/cls' + name] = self.head_loss.last_scale

        for extra_loss_name, extra_loss in self.losses_extra.items():
            losses[extra_loss_name.replace('_', '/') + name] = extra_loss(
                norm_embd, cls_score, labels)

        if self.with_embedding:
            losses.update(self.fc_angular.loss(name))

        return losses

    @property
    def last_scale(self):
        if hasattr(self.head_loss, 'last_scale'):
            return self.head_loss.last_scale
        else:
            return None
