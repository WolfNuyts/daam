from pathlib import Path
from typing import List, Type, Any, Dict, Tuple, Union
import math

from diffusers import StableDiffusionPipeline
from diffusers.models.attention import CrossAttention
import numpy as np
import PIL.Image as Image
import torch
import torch.nn.functional as F
from matplotlib import pyplot as plt

from .utils import cache_dir, auto_autocast
from .experiment import GenerationExperiment
from .heatmap import RawHeatMapCollection, GlobalHeatMap
from .hook import ObjectHooker, AggregateHooker, UNetCrossAttentionLocator


__all__ = ['trace', 'DiffusionHeatMapHooker', 'GlobalHeatMap']


class DiffusionHeatMapHooker(AggregateHooker):
    def __init__(
            self,
            pipeline:
            StableDiffusionPipeline,
            low_memory: bool = False,
            load_heads: bool = False,
            save_heads: bool = False,
            data_dir: str = None,
            foc_att_args=None,
            foc_att_mask=None
    ):
        self.all_heat_maps = RawHeatMapCollection()
        h = (pipeline.unet.config.sample_size * pipeline.vae_scale_factor)
        self.latent_hw = 4096 if h == 512 else 9216  # 64x64 or 96x96 depending on if it's 2.0-v or 2.0
        locate_middle = load_heads or save_heads
        self.locator = UNetCrossAttentionLocator(restrict={0} if low_memory else None, locate_middle_block=locate_middle)
        self.last_prompt: str = ''
        self.last_image: Image = None
        self.time_idx = 0
        self._gen_idx = 0

        modules = [
            UNetCrossAttentionHooker(
                x,
                self,
                layer_idx=idx,
                latent_hw=self.latent_hw,
                load_heads=load_heads,
                save_heads=save_heads,
                data_dir=data_dir,
                foc_att_args=foc_att_args,
                foc_att_mask=foc_att_mask,
            ) for idx, x in enumerate(self.locator.locate(pipeline.unet))
        ]

        modules.append(PipelineHooker(pipeline, self))
        self.cross_att_hookers = modules[:-1]

        super().__init__(modules)
        self.pipe = pipeline

    def time_callback(self, *args, **kwargs):
        self.time_idx += 1

    @property
    def layer_names(self):
        return self.locator.layer_names

    def to_experiment(self, path, seed=None, id='.', subtype='.', **compute_kwargs):
        # type: (Union[Path, str], int, str, str, Dict[str, Any]) -> GenerationExperiment
        """Exports the last generation call to a serializable generation experiment."""

        return GenerationExperiment(
            self.last_image,
            self.compute_global_heat_map(**compute_kwargs).heat_maps,
            self.last_prompt,
            seed=seed,
            id=id,
            subtype=subtype,
            path=path,
            tokenizer=self.pipe.tokenizer,
        )

    def compute_global_heat_map(self, prompt=None, factors=None, head_idx=None, layer_idx=None, normalize=False):
        # type: (str, List[float], int, int, bool) -> GlobalHeatMap
        """
        Compute the global heat map for the given prompt, aggregating across time (inference steps) and space (different
        spatial transformer block heat maps).

        Args:
            prompt: The prompt to compute the heat map for. If none, uses the last prompt that was used for generation.
            factors: Restrict the application to heat maps with spatial factors in this set. If `None`, use all sizes.
            head_idx: Restrict the application to heat maps with this head index. If `None`, use all heads.
            layer_idx: Restrict the application to heat maps with this layer index. If `None`, use all layers.

        Returns:
            A heat map object for computing word-level heat maps.
        """
        heat_maps = self.all_heat_maps

        if prompt is None:
            prompt = self.last_prompt

        if factors is None:
            factors = {0, 1, 2, 4, 8, 16, 32, 64}
        else:
            factors = set(factors)

        all_merges = []
        x = int(np.sqrt(self.latent_hw))

        with auto_autocast(dtype=torch.float32):
            for (factor, layer, head), heat_map in heat_maps:
                if factor in factors and (head_idx is None or head_idx == head) and (layer_idx is None or layer_idx == layer):
                    heat_map = heat_map.unsqueeze(1)
                    # The clamping fixes undershoot.
                    all_merges.append(F.interpolate(heat_map, size=(x, x), mode='bicubic').clamp_(min=0))

            try:
                maps = torch.stack(all_merges, dim=0)
            except RuntimeError:
                if head_idx is not None or layer_idx is not None:
                    raise RuntimeError('No heat maps found for the given parameters.')
                else:
                    raise RuntimeError('No heat maps found. Did you forget to call `with trace(...)` during generation?')

            maps = maps.mean(0)[:, 0]
            maps = maps[:len(self.pipe.tokenizer.tokenize(prompt)) + 2]  # 1 for SOS and 1 for padding

            if normalize:
                maps = maps / (maps[1:-1].sum(0, keepdim=True) + 1e-6)  # drop out [SOS] and [PAD] for proper probabilities

        return GlobalHeatMap(self.pipe.tokenizer, prompt, maps)

    def compute_word_attention_importance(self):
        nb_layers, nb_steps, nb_words = len(self.cross_att_hookers), len(self.cross_att_hookers[0].att_cum[0]), len(self.cross_att_hookers[0].att_cum)
        res = np.zeros((nb_words, nb_steps, nb_layers))
        res_unc = np.zeros((nb_words, nb_steps, nb_layers))
        for module in self.cross_att_hookers:
            for word_idx in module.att_cum:
                res[word_idx, :, module.layer_idx] = np.array(module.att_cum[word_idx])
                res_unc[word_idx, :, module.layer_idx] = np.array(module.att_cum_unc[word_idx])
        return res, res_unc

    def get_attention_maps(self):
        res = torch.tensor([])
        for module in self.cross_att_hookers:
            mod_maps = torch.stack(module.att_maps, dim=0).unsqueeze(0).mean(dim=-3)
            res = torch.cat([res, mod_maps], dim=0)

        res = res.permute(2,1,0,3,4) #tokens, steps, layers, height, width
        return res


class PipelineHooker(ObjectHooker[StableDiffusionPipeline]):
    def __init__(self, pipeline: StableDiffusionPipeline, parent_trace: 'trace'):
        super().__init__(pipeline)
        self.heat_maps = parent_trace.all_heat_maps
        self.parent_trace = parent_trace

    def _hooked_run_safety_checker(hk_self, self: StableDiffusionPipeline, image, *args, **kwargs):
        image, has_nsfw = hk_self.monkey_super('run_safety_checker', image, *args, **kwargs)
        pil_image = self.numpy_to_pil(image)
        hk_self.parent_trace.last_image = pil_image[0]

        return image, has_nsfw

    def _hooked_encode_prompt(hk_self, _: StableDiffusionPipeline, prompt: Union[str, List[str]], *args, **kwargs):
        if not isinstance(prompt, str) and len(prompt) > 1:
            raise ValueError('Only single prompt generation is supported for heat map computation.')
        elif not isinstance(prompt, str):
            last_prompt = prompt[0]
        else:
            last_prompt = prompt

        hk_self.heat_maps.clear()
        hk_self.parent_trace.last_prompt = last_prompt
        ret = hk_self.monkey_super('_encode_prompt', prompt, *args, **kwargs)

        return ret

    def _hook_impl(self):
        self.monkey_patch('run_safety_checker', self._hooked_run_safety_checker)
        self.monkey_patch('_encode_prompt', self._hooked_encode_prompt)


class UNetCrossAttentionHooker(ObjectHooker[CrossAttention]):
    def __init__(
            self,
            module: CrossAttention,
            parent_trace: 'trace',
            context_size: int = 77,
            layer_idx: int = 0,
            latent_hw: int = 9216,
            load_heads: bool = False,
            save_heads: bool = False,
            data_dir: Union[str, Path] = None,
            foc_att_args=None,
            foc_att_mask=None,
    ):
        super().__init__(module)
        self.heat_maps = parent_trace.all_heat_maps
        self.context_size = context_size
        self.layer_idx = layer_idx
        self.latent_hw = latent_hw

        self.load_heads = load_heads
        self.save_heads = save_heads
        self.trace = parent_trace

        self.foc_att_args = foc_att_args
        self.foc_att_mask = foc_att_mask

        self.att_cum = {}
        self.att_cum_unc = {}
        self.att_maps = []

        if data_dir is not None:
            data_dir = Path(data_dir)
        else:
            data_dir = cache_dir() / 'heads'

        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)


    def _show_att_map(self, x):
        h = w = int(math.sqrt(x.size(1)))
        x = x[x.size(0)//2 :].mean(axis=0).squeeze(0)
        x = x.permute(1, 0)
        for i, map_ in enumerate(x):
            map_ = map_.view(h, w).cpu().numpy()
            plt.imshow(map_, cmap='hot', interpolation='nearest')
            plt.colorbar()
            plt.title(i)
            plt.show()

    @torch.no_grad()
    def _unravel_attn(self, x):
        # type: (torch.Tensor) -> torch.Tensor
        # x shape: (heads, height * width, tokens)
        """
        Unravels the attention, returning it as a collection of heat maps.

        Args:
            x (`torch.Tensor`): cross attention slice/map between the words and the tokens.
            value (`torch.Tensor`): the value tensor.

        Returns:
            `List[Tuple[int, torch.Tensor]]`: the list of heat maps across heads.
        """
        h = w = int(math.sqrt(x.size(1)))
        maps = []
        x = x.permute(2, 0, 1)

        with auto_autocast(dtype=torch.float32):
            for map_ in x:
                map_ = map_.view(map_.size(0), h, w)
                map_ = map_[map_.size(0) // 2:]  # Filter out unconditional
                maps.append(map_)

        maps = torch.stack(maps, 0)  # shape: (tokens, heads, height, width)
        return maps.permute(1, 0, 2, 3).contiguous()  # shape: (heads, tokens, height, width)

    def _hooked_sliced_attention(hk_self, self, query, key, value, sequence_length, dim, attention_mask):
        batch_size_attention = query.shape[0]
        hidden_states = torch.zeros(
            (batch_size_attention, sequence_length, dim // self.heads), device=query.device, dtype=query.dtype
        )
        slice_size = self._slice_size if self._slice_size is not None else hidden_states.shape[0]
        for i in range(hidden_states.shape[0] // slice_size):
            start_idx = i * slice_size
            end_idx = (i + 1) * slice_size
            attn_slice = torch.baddbmm(
                torch.empty(slice_size, query.shape[1], key.shape[1], dtype=query.dtype, device=query.device),
                query[start_idx:end_idx],
                key[start_idx:end_idx].transpose(-1, -2),
                beta=0,
                alpha=self.scale,
            )
            attn_slice = attn_slice.softmax(dim=-1)
            factor = int(math.sqrt(hk_self.latent_hw // attn_slice.shape[1]))

            if attn_slice.shape[-1] <= hk_self.context_size:
                # shape: (batch_size, 64 // factor, 64 // factor, 77)
                maps = hk_self._unravel_attn(attn_slice)

                for head_idx, heatmap in enumerate(maps):
                    hk_self.heat_maps.update(factor, hk_self.layer_idx, head_idx, heatmap)

            attn_slice = torch.bmm(attn_slice, value[start_idx:end_idx])

            hidden_states[start_idx:end_idx] = attn_slice

        # reshape hidden_states
        hidden_states = self.reshape_batch_dim_to_heads(hidden_states)
        return hidden_states

    def _save_attn(self, attn_slice: torch.Tensor):
        torch.save(attn_slice, self.data_dir / f'{self.trace._gen_idx}.pt')

    def _load_attn(self) -> torch.Tensor:
        return torch.load(self.data_dir / f'{self.trace._gen_idx}.pt')

    def _hooked_attention(hk_self, self, query, key, value, attention_mask):
        """
        Monkey-patched version of :py:func:`.CrossAttention._attention` to capture attentions and aggregate them.

        Args:
            hk_self (`UNetCrossAttentionHooker`): pointer to the hook itself.
            self (`CrossAttention`): pointer to the module.
            query (`torch.Tensor`): the query tensor.
            key (`torch.Tensor`): the key tensor.
            value (`torch.Tensor`): the value tensor.
        """

        attention_scores = torch.baddbmm(
            torch.empty(query.shape[0], query.shape[1], key.shape[1], dtype=query.dtype, device=query.device),
            query,
            key.transpose(-1, -2),
            beta=0,
            alpha=self.scale,
        )
        attn_slice = attention_scores.softmax(dim=-1)

        if hk_self.save_heads:
            hk_self._save_attn(attn_slice)
        elif hk_self.load_heads:
            attn_slice = hk_self._load_attn()

        if hk_self.foc_att_args.save_cum_att:
            att_cum_unc = attn_slice[:attn_slice.size(0) // 2].mean(dim=[0,1])
            att_cum = attn_slice[attn_slice.size(0) // 2:].mean(dim=[0,1])
            maps = hk_self._unravel_attn(attn_slice).transpose(0,1) #tokens, heads, height, width
            maps = F.interpolate(maps, size=(64, 64), mode='bicubic')
            hk_self.att_maps.append(maps.cpu())
            for i in range(att_cum.size(0)):
                hk_self.att_cum.setdefault(i, []).append(att_cum[i].item())
                hk_self.att_cum_unc.setdefault(i, []).append(att_cum_unc[i].item())


        factor = int(math.sqrt(hk_self.latent_hw // attn_slice.shape[1]))
        hk_self.trace._gen_idx += 1

        if attn_slice.shape[-1] <= hk_self.context_size and factor != 8:
            # shape: (batch_size, 64 // factor, 64 // factor, 77)
            maps = hk_self._unravel_attn(attn_slice)

            for head_idx, heatmap in enumerate(maps):
                hk_self.heat_maps.update(factor, hk_self.layer_idx, head_idx, heatmap)

        # compute attention output
        hidden_states = torch.bmm(attn_slice, value)

        # reshape hidden_states
        hidden_states = self.reshape_batch_dim_to_heads(hidden_states)
        return hidden_states

    def _hooked_focused_attention(hk_self, self, query, key, value, focused_attention_mask, focused_attention_norm, attention_mask):
        maximize_over_heads =  hk_self.foc_att_args.maximize_over_heads
        maximize_with_mean =  hk_self.foc_att_args.maximize_with_mean
        step_value =  hk_self.foc_att_args.step_value
        foc_multiplier = hk_self.foc_att_args.foc_mupltiplier
        replace_att =  hk_self.foc_att_args.replace_att
        up_low_att = False # for a specific test that attends over uper and lower side of the image only for certain words

        focused_attention_mask, w_mask = focused_attention_mask
        focused_attention_mask, w_mask = torch.repeat_interleave(focused_attention_mask, self.heads, dim=0), \
                                         torch.repeat_interleave(w_mask, self.heads, dim=0)

        attention_scores = torch.baddbmm(
            torch.empty(query.shape[0], query.shape[1], key.shape[1], dtype=query.dtype, device=query.device),
            query,
            key.transpose(-1, -2),
            beta=0,
            alpha=self.scale,
        )
        focus_weights = torch.bmm(attention_scores, focused_attention_mask)


        # only active on the activated words -> need for better way of handling
        if hk_self.foc_att_mask is not None:
            dim_size = int(math.sqrt(attention_scores.size(1)))
            assert hk_self.foc_att_mask.size(-1) % dim_size == 0
            ds_ratio = hk_self.foc_att_mask.size(-1) // dim_size
            if ds_ratio > 1:
                maxpool = torch.nn.MaxPool2d(ds_ratio, stride=ds_ratio)
                hk_self.foc_att_mask = maxpool(hk_self.foc_att_mask)
            foc_att_mask = hk_self.foc_att_mask.view(1, key.size(1), dim_size**2)
            foc_att_mask = torch.repeat_interleave(foc_att_mask, self.heads, dim=0)
            foc_att_mask = torch.vstack([torch.zeros_like(foc_att_mask), foc_att_mask]).transpose(1,2).to(focused_attention_mask.device)
            focus_weights = torch.bmm(foc_att_mask, focused_attention_mask)
            focus_weights = torch.clamp(focus_weights, min=0)
            focus_weights /= (focus_weights[:, :, :].max(dim=1, keepdim=True)[0] + 1e-6)
            if step_value is not None:
                focus_weights = torch.where(focus_weights > step_value, torch.ones_like(focus_weights), torch.zeros_like(focus_weights))
            focus_weights *= foc_multiplier
            focus_weights = torch.maximum(focus_weights, torch.logical_not(w_mask)[:, None, :])
        elif up_low_att:
            w_mask[w_mask.size(0)//2:, 5] = 1
            w_mask[w_mask.size(0)//2:, 6] = 1
            w_mask[w_mask.size(0)//2:, 8] = 2
            w_mask[w_mask.size(0)//2:, 9] = 2
            if w_mask.size(1) == 16:
                w_mask[w_mask.size(0) // 2:, 11] = 1
                w_mask[w_mask.size(0) // 2:, 12] = 2
            mask =  ~torch.repeat_interleave(w_mask.bool().unsqueeze(1), focus_weights.size(1), dim=1)
            focus_weights = torch.repeat_interleave(w_mask.unsqueeze(1), focus_weights.size(1), dim=1)
            focus_weights[:, :focus_weights.size(1) // 2][focus_weights[:, :focus_weights.size(1) // 2] == 2] = 0
            focus_weights[:, focus_weights.size(1) // 2:][focus_weights[:, focus_weights.size(1) // 2:] == 1] = 0
            focus_weights[focus_weights > 0] = 1
            focus_weights[mask] = 1

        elif replace_att:
            mask =  ~torch.repeat_interleave(w_mask.bool().unsqueeze(1), focus_weights.size(1), dim=1)
            focus_weights[mask] = attention_scores[mask]
            attention_scores = torch.ones_like(attention_scores)
        else:
            focus_weights = torch.clamp(focus_weights, min=0)
            if focused_attention_norm is not None:
                focus_weights = focused_attention_norm(focus_weights)
            # use min-max normalization
            else:
                focus_weights /= (attention_scores[:, :, 1:].sum(dim=-1, keepdim=True) + 1e-6)
                #focus_weights /= attention_scores[:, :, 1:].max(dim=-1, keepdim=True).values
            focus_weights = torch.maximum(focus_weights, torch.logical_not(w_mask)[:, None, :])

            if maximize_over_heads:
                focus_weights = focus_weights.reshape(-1, self.heads, *focus_weights.shape[1:])
                if maximize_with_mean:
                    focus_weights = focus_weights.mean(dim=1)
                else:
                    focus_weights = focus_weights.max(dim=1)[0]
                focus_weights = torch.repeat_interleave(focus_weights, self.heads, dim=0)

            if step_value is not None:
                focus_weights = torch.where(focus_weights > step_value, torch.ones_like(focus_weights), torch.zeros_like(focus_weights))

        #focus_weights /= focus_weights.max(dim=1, keepdim=True).values

        #hk_self._show_att_map(focus_weights)
        attn_slice = (focus_weights * attention_scores).softmax(dim=-1)

        if hk_self.save_heads:
            hk_self._save_attn(attn_slice)
        elif hk_self.load_heads:
            attn_slice = hk_self._load_attn()

        if hk_self.foc_att_args.save_cum_att:
            att_cum_unc = attn_slice[:attn_slice.size(0) // 2].mean(dim=[0, 1])
            att_cum = attn_slice[attn_slice.size(0) // 2:].mean(dim=[0, 1])
            maps = hk_self._unravel_attn(attn_slice).transpose(0, 1)  # tokens, heads, height, width
            maps = F.interpolate(maps, size=(64, 64), mode='bicubic')
            hk_self.att_maps.append(maps.cpu())
            for i in range(att_cum.size(0)):
                hk_self.att_cum.setdefault(i, []).append(att_cum[i].item())
                hk_self.att_cum_unc.setdefault(i, []).append(att_cum_unc[i].item())

        factor = int(math.sqrt(hk_self.latent_hw // attn_slice.shape[1]))
        hk_self.trace._gen_idx += 1

        if attn_slice.shape[-1] <= hk_self.context_size and factor != 8:
            # shape: (batch_size, 64 // factor, 64 // factor, 77)
            maps = hk_self._unravel_attn(attn_slice)

            for head_idx, heatmap in enumerate(maps):
                hk_self.heat_maps.update(factor, hk_self.layer_idx, head_idx, heatmap)

        # compute attention output
        hidden_states = torch.bmm(attn_slice, value)

        # reshape hidden_states
        hidden_states = self.reshape_batch_dim_to_heads(hidden_states)
        return hidden_states

    def _hook_impl(self):
        self.monkey_patch('_focused_attention', self._hooked_focused_attention)
        self.monkey_patch('_attention', self._hooked_attention)
        self.monkey_patch('_sliced_attention', self._hooked_sliced_attention)

    @property
    def num_heat_maps(self):
        return len(next(iter(self.heat_maps.values())))


trace: Type[DiffusionHeatMapHooker] = DiffusionHeatMapHooker
