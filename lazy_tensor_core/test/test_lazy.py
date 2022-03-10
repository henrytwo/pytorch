import torch

from torch.testing._internal.common_utils import run_tests, TestCase
from torch.testing._internal.jit_utils import JitTestCase
from torch.testing._internal.common_methods_invocations import op_db
from torch.testing._internal.common_device_type import ops, instantiate_device_type_tests

import lazy_tensor_core
lazy_tensor_core._LAZYC._ltc_init_ts_backend()
import lazy_tensor_core.core.lazy_model as ltm
import lazy_tensor_core.debug.metrics as metrics
import itertools
import yaml
import os
import pathlib

def remove_suffixes(l):
    return [x.split(".")[0] for x in l]

def init_lists():
    path_to_script = pathlib.Path(os.path.abspath(os.path.dirname(__file__)))
    TS_NATIVE_FUNCTIONS_PATH = os.path.join(*path_to_script.parts[:-1], "ts_native_functions.yaml")
    yaml_ts = yaml.load(open(TS_NATIVE_FUNCTIONS_PATH), yaml.Loader)
    LAZY_OPS_LIST = set(remove_suffixes(itertools.chain(yaml_ts["full_codegen"], yaml_ts["supported"], yaml_ts["autograd"])))
    FALLBACK_LIST = set(["clamp"])
    BAD_OPS_LIST = set([
        'index_select',  # Empty output_sizes is not supported
        'clone',  # is clone decomposed?
    ])

    return (LAZY_OPS_LIST, FALLBACK_LIST, BAD_OPS_LIST)

(LAZY_OPS_LIST, FALLBACK_LIST, BAD_OPS_LIST) = init_lists()

torch.manual_seed(42)

class TestLazyTensor(JitTestCase):
    def testConvolutionBackward(self):
        def clone_move(t):
            dev = 'lazy'
            copy_t = t.detach().clone().requires_grad_(True).to(device=dev)
            return copy_t

        inp = torch.rand(1, 3, 128, 128, device='cuda', requires_grad=True)
        inp_copy = clone_move(inp)
        grad = torch.rand(1, 32, 121, 121, device='cuda')  # no requires_grad
        grad_copy = clone_move(grad)
        weight = torch.rand(32, 3, 8, 8, device='cuda', requires_grad=True)
        weight_copy = clone_move(weight)
        bias = torch.rand(32, device='cuda', requires_grad=True)
        bias_copy = clone_move(bias)

        # run eager
        conv_out = torch.nn.functional.conv2d(inp, weight, bias)
        (inp_grad, weight_grad, bias_grad) = torch.autograd.grad([conv_out], [inp, weight, bias], [grad])

        # run lazy
        conv_copy_out = torch.nn.functional.conv2d(inp_copy, weight_copy, bias_copy)
        (inp_copy_grad, weight_copy_grad, bias_copy_grad) = torch.autograd.grad(
            [conv_copy_out], [inp_copy, weight_copy, bias_copy], [grad_copy])

        jit_graph = lazy_tensor_core._LAZYC._get_ltc_tensors_backend([bias_copy_grad])

        # check numerics
        torch.testing.assert_allclose(bias_copy_grad.cpu(), bias_grad.cpu())
        torch.testing.assert_allclose(weight_copy_grad.cpu(), weight_grad.cpu())
        torch.testing.assert_allclose(inp_copy_grad.cpu(), inp_grad.cpu())

class TestLazyOpInfo(TestCase):
    @ops([op for op in op_db if op.name in LAZY_OPS_LIST and op.name not in BAD_OPS_LIST], allowed_dtypes=(torch.float,))
    def test_dispatched_to_lazy(self, device, dtype, op):

        def get_name(op):
            l = [op.name]
            if op.variant_test_name != '':
                l.append(op.variant_test_name)
            return '.'.join(l)

        global FALLBACK_LIST
        samples = op.sample_inputs("lazy", dtype, requires_grad=False)
        sample = list(samples)[0]
        args = [sample.input] + list(sample.args)
        kwargs = sample.kwargs
        ltm.mark_step()
        ltm.wait_device_ops()
        metrics.reset_metrics()

        r = op(*args, **kwargs)
        ltm.mark_step()
        ltm.wait_device_ops()
        prefix = "aten" if op.name in FALLBACK_LIST else "lazy"
        found = f"{prefix}::{op.name}" in remove_suffixes(metrics.counter_names())
        # check aliases
        if not found:
            for alias in op.aliases:
                alias_found = f"{prefix}::{alias.name}" in remove_suffixes(metrics.counter_names())
                found = found or alias_found
                if found:
                    break
        self.assertTrue(found)

# TODO: after we move to master, add Lazy as a new Device here:
# https://github.com/pytorch/pytorch/blob/master/torch/testing/_internal/common_device_type.py#L532
instantiate_device_type_tests(TestLazyOpInfo, globals(), only_for="cpu")


if __name__ == '__main__':
    run_tests()