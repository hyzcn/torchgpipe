from functools import partial

import pytest
import torch
from torch import nn
import torch.cuda

from torchgpipe.checkpoint import Checkpointing, checkpoint
from torchgpipe.dependency import fork, join
from torchgpipe.microbatch import Batch

devices = ['cpu']
if torch.cuda.is_available():
    devices.append('cuda')


@pytest.mark.parametrize('device', devices)
def test_serial_checkpoints(device):
    # Copied from https://github.com/pytorch/pytorch/pull/18568.
    timeline = []

    class Log(torch.autograd.Function):
        @staticmethod
        def forward(ctx, name, x):
            ctx.name = name
            timeline.append(f'{name}:forward')
            return x.detach()

        @staticmethod
        def backward(ctx, grad_output):
            name = ctx.name
            timeline.append(f'{name}:backward')
            return None, grad_output

    a = torch.rand(1, device=device, requires_grad=True)
    b = torch.rand(1, device=device, requires_grad=True)

    # Increase the next function sequence number.
    _ = a + 1 + 2 + 3 + 4 + 5

    a = checkpoint(partial(Log.apply, 'a'), a)

    a, phony = fork(a)
    b = join(b, phony)

    b = checkpoint(partial(Log.apply, 'b'), b)

    c = torch.cat((a, b))

    out = c.sum()

    #                        +--> {a} --Checkpoint(Log)--> {a}
    # {out} --Sum--> {c} --Cat     ^-----------------------------+
    #                        +--> {b} --Checkpoint(Log)--> {b} --First--> {b}
    out.backward()

    assert timeline == \
        ['a:forward', 'b:forward', 'b:forward', 'b:backward', 'a:forward', 'a:backward']
    #    |----------------------|  |-----------------------|  |-----------------------|
    #          forward pass            Checkpoint(Log[b])         Checkpoint(Log[a])


def test_not_requires_grad():
    x = Batch(torch.rand(1, requires_grad=False))
    assert not x[0].requires_grad

    def f(x):
        return x * 2

    chk = Checkpointing(f, x)
    x = chk.checkpoint()
    assert x[0].requires_grad

    chk.recompute(x)
    assert x[0].requires_grad

    x.tensor.backward()


def test_not_requires_grad_with_parameter():
    x = torch.rand(1, requires_grad=False)
    a = torch.rand(1, requires_grad=True)

    def f(x):
        return x * a

    y = checkpoint(f, x)
    y.backward()

    assert a.grad is not None


@pytest.mark.parametrize('device', devices)
def test_random_in_checkpoint(device):
    dropout = nn.Dropout(p=0.5)

    torch.manual_seed(0)
    x = torch.randn(3, 3, device=device, requires_grad=True)
    y = dropout(x)
    y.norm().backward()

    torch.manual_seed(0)
    chk_x = torch.randn(3, 3, device=device, requires_grad=True)
    chk_y = checkpoint(dropout, chk_x)
    chk_y.norm().backward()

    assert torch.allclose(x.grad, chk_x.grad)
