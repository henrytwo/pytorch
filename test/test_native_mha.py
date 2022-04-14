import math

import torch
from torch.testing._internal.common_device_type import (
    dtypes,
    dtypesIfCUDA,
    instantiate_device_type_tests,
    onlyCUDA,
    skipMeta,
)
from torch.testing._internal.common_utils import TestCase

class TestMHADeviceType(TestCase):
    @torch.inference_mode()
    def _test_transform_bias_rescale_qkv_impl(
        self, device, dtype, use_nt, use_padding=False
    ):
        tests = [
            (64, 4, 16, 8),
            # dim_per_head = 12 does not divide evenly by CPU vectorization length of 8
            (24, 2, 4, 2),
            # Make sure CUDA can handle small input sizes
            (2, 2, 2, 2),
            # dim_per_head = 6 does not divide evenly by CUDA vectorization length of 4, causes alignment issues
            (24, 4, 4, 2),
            (48, 4, 16, 8),
        ]
        for (embed_dim, num_heads, bs, sl) in tests:
            torch.manual_seed(9343)
            dense_x = x = (
                torch.randn(bs, sl, 3 * embed_dim, device=device, dtype=dtype) * 10
            )
            if use_padding:
                x[0][-1] = torch.full(x[0][-1].shape, float("-Inf"))
            if use_nt:
                xs = list(torch.unbind(x))
                if use_padding:
                    xs[0] = xs[0][:-1]
                x = torch.nested_tensor(xs, device=device, dtype=dtype)
            qkv = torch.nn.Linear(embed_dim, 3 * embed_dim, device=device, dtype=dtype)

            with torch.no_grad():
                (q, k, v) = torch.ops.nativetransformers._transform_bias_rescale_qkv_op(
                    x, qkv.bias, num_head=num_heads
                )

                def simple_transform_bias_rescale_qkv(qkv, bias):
                    (q, k, v) = torch.split(qkv, embed_dim, dim=-1)
                    (q_bias, k_bias, v_bias) = torch.split(bias, embed_dim, dim=-1)
                    return tuple(
                        x.reshape(
                            (bs, sl, num_heads, embed_dim // num_heads)
                        ).transpose(2, 1)
                        for x in (
                            (q + q_bias) / math.sqrt(embed_dim // num_heads),
                            (k + k_bias),
                            (v + v_bias),
                        )
                    )

                correct_q, correct_k, correct_v = simple_transform_bias_rescale_qkv(
                    dense_x, qkv.bias
                )
                if use_nt and use_padding:
                    for t in (correct_q, correct_k, correct_v):
                        t[t == float("-Inf")] = 0

            self.assertEqual(q.size(), correct_q.size())
            torch.testing.assert_close(q, correct_q)
            torch.testing.assert_close(k, correct_k)
            torch.testing.assert_close(v, correct_v)

    @dtypesIfCUDA(torch.float)
    @dtypes(torch.float)
    @skipMeta
    def test_transform_bias_rescale_qkv(self, device, dtype):
        for use_padding in (False, True):
            self._test_transform_bias_rescale_qkv_impl(
                device, dtype, use_nt=False, use_padding=use_padding
            )

    @dtypesIfCUDA(torch.float)
    @dtypes(torch.float)
    @skipMeta
    @onlyCUDA
    def test_transform_bias_rescale_qkv_nested(self, device, dtype):
        for use_padding in (False, True):
            self._test_transform_bias_rescale_qkv_impl(
                device, dtype, use_nt=True, use_padding=use_padding
            )

    def _test_multihead_attention_impl(
        self, device, dtype, mode, use_nt, use_padding=False
    ):
        embed_dim = 64
        num_heads = 4
        bs = 16
        sl = 8

        q = torch.randn(bs, sl, embed_dim, device=device, dtype=dtype) * 10
        if use_padding:
            q[0][-1] = torch.zeros_like(q[0][-1], device=device, dtype=dtype)
            mask = torch.zeros(q.shape[:-1], device=device, dtype=torch.bool)
            mask[0][-1] = True
        if mode == "self":
            k = q
            v = q
        elif mode == "encdec":
            k = torch.randn(bs, sl, embed_dim, device=device, dtype=dtype) * 10
            v = k
        elif mode == "generic":
            k = torch.randn(bs, sl, embed_dim, device=device, dtype=dtype) * 10
            v = torch.randn(bs, sl, embed_dim, device=device, dtype=dtype) * 10
        else:
            self.fail(f"invalid mode `{mode}`!")

        qkv = torch.nn.Linear(embed_dim, 3 * embed_dim, device=device, dtype=dtype)
        proj = torch.nn.Linear(embed_dim, embed_dim, device=device, dtype=dtype)

        pt = torch.nn.MultiheadAttention(
            embed_dim, num_heads, batch_first=True, device=device, dtype=dtype
        )
        pt.in_proj_weight = qkv.weight
        pt.in_proj_bias = qkv.bias
        pt.out_proj.weight = proj.weight
        pt.out_proj.bias = proj.bias

        class NativeMHA(torch.nn.Module):
            def __init__(self, embed_dim, num_heads, qkv, proj):
                super().__init__()
                self.qkv = qkv
                self.proj = proj
                self.embed_dim = embed_dim
                self.num_heads = num_heads

            def forward(self, q, k, v, key_padding_mask):
                return torch.ops.nativetransformers._native_multi_head_attention(
                    q,
                    k,
                    v,
                    self.embed_dim,
                    self.num_heads,
                    self.qkv.weight,
                    self.qkv.bias,
                    self.proj.weight,
                    self.proj.bias,
                    key_padding_mask,
                    need_weights=True,
                    average_attn_weights=False,
                )

        npt = NativeMHA(
            embed_dim=embed_dim, num_heads=num_heads, qkv=qkv, proj=proj
        ).to(dtype)

        if device == "cuda":
            pt = pt.cuda()
            npt = npt.cuda()

        ypt, weight_pt = pt(
            q,
            k,
            v,
            need_weights=True,
            average_attn_weights=False,
            key_padding_mask=mask if use_padding else None,
        )
        if use_nt:
            qs = list(torch.unbind(q))
            if use_padding:
                qs[0] = qs[0][:-1]
            q = torch.nested_tensor(qs, device=device, dtype=dtype)
            if mode == "self":
                k = v = q
            elif mode == "encdec":
                k = torch.nested_tensor(torch.unbind(k), device=device, dtype=dtype)
                v = k
            else:
                k = torch.nested_tensor(torch.unbind(k), device=device, dtype=dtype)
                v = torch.nested_tensor(torch.unbind(v), device=device, dtype=dtype)

        ynpt, weight_npt = npt(
            q, k, v, key_padding_mask=mask if use_padding and not use_nt else None
        )
        if use_nt:
            ynpt = ynpt.to_padded_tensor(0)

        # PyTorch implementation returns non-zero junk in the padding
        # locations; overwrite it so that the comparison works out.
        if use_padding:
            ypt[0][-1] = torch.zeros_like(ypt[0][-1], device=device, dtype=dtype)
            ynpt[0][-1] = torch.zeros_like(ynpt[0][-1], device=device, dtype=dtype)
            # Zero the last row of each TxT weight matrix
            for nh in range(num_heads):
                weight_pt[0][nh][-1] = torch.zeros_like(weight_pt[0][nh][-1], device=device, dtype=dtype)
                weight_npt[0][nh][-1] = torch.zeros_like(weight_npt[0][nh][-1], device=device, dtype=dtype)

        if dtype == torch.half:
            torch.testing.assert_close(ypt, ynpt, atol=1e-3, rtol=1e-3)
        else:
            torch.testing.assert_close(ypt, ynpt)

        torch.testing.assert_close(weight_pt, weight_npt)

    @dtypesIfCUDA(torch.float, torch.half)
    @dtypes(torch.float)
    @skipMeta
    @torch.inference_mode()
    def test_native_multihead_self_attention(self, device, dtype):
        for use_padding in (False, True):
            for use_nt in (False, True):
                self._test_multihead_attention_impl(
                    device,
                    dtype,
                    "self",
                    use_nt=use_nt,
                    use_padding=use_padding,
                )

    @dtypesIfCUDA(torch.float, torch.half)
    @dtypes(torch.float)
    @skipMeta
    @torch.inference_mode()
    def test_native_multihead_encoder_decoder_attention(self, device, dtype):
        self._test_multihead_attention_impl(
            device, dtype, "encdec", use_nt=False
        )

    @dtypesIfCUDA(torch.float, torch.half)
    @dtypes(torch.float)
    @skipMeta
    @torch.inference_mode()
    def test_native_multihead_attention(self, device, dtype):
        self._test_multihead_attention_impl(
            device, dtype, "generic", use_nt=False
        )


instantiate_device_type_tests(TestMHADeviceType, globals())
