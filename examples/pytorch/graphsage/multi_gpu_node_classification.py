import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchmetrics.functional as MF
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel
import torch.multiprocessing as mp
import dgl
import dgl.nn as dglnn
from dgl.multiprocessing import shared_tensor
from dgl.data import AsNodePredDataset
from dgl.dataloading import DataLoader, NeighborSampler, MultiLayerFullNeighborSampler
from ogb.nodeproppred import DglNodePropPredDataset
import tqdm
import argparse

class SAGE(nn.Module):
    def __init__(self, in_size, hid_size, out_size):
        super().__init__()
        self.layers = nn.ModuleList()
        # three-layer GraphSAGE-mean
        self.layers.append(dglnn.SAGEConv(in_size, hid_size, 'mean'))
        self.layers.append(dglnn.SAGEConv(hid_size, hid_size, 'mean'))
        self.layers.append(dglnn.SAGEConv(hid_size, out_size, 'mean'))
        self.dropout = nn.Dropout(0.5)
        self.hid_size = hid_size
        self.out_size = out_size

    def forward(self, blocks, x):
        h = x
        for l, (layer, block) in enumerate(zip(self.layers, blocks)):
            h = layer(block, h)
            if l != len(self.layers) - 1:
                h = F.relu(h)
                h = self.dropout(h)
        return h
    
    def inference(self, g, device, batch_size, use_uva):
        feat = g.ndata['feat']
        sampler = MultiLayerFullNeighborSampler(1, prefetch_node_feats=['feat'])
        dataloader = DataLoader(
            g, torch.arange(g.num_nodes(), device=device), sampler, device=device,
            batch_size=batch_size, shuffle=False, drop_last=False,
            num_workers=0, use_ddp=True, use_uva=use_uva)
        feat = feat.to(device)            
        for l, layer in enumerate(self.layers):
            # in order to prevent running out of GPU memory, allocate a
            # shared output tensor 'y' in host memory
            y = shared_tensor(
                    (g.num_nodes(), self.hid_size if l != len(self.layers) - 1 else self.out_size))
            for input_nodes, output_nodes, blocks in tqdm.tqdm(dataloader) \
                    if dist.get_rank() == 0 else dataloader:
                x = feat[input_nodes]
                h = layer(blocks[0], x) # len(blocks) = 1
                if l != len(self.layers) - 1:
                    h = F.relu(h)
                    h = self.dropout(h)
                # by design, our output nodes are contiguous
                y[output_nodes] = h.to(y.device)
            # make sure all GPUs are done writing to 'y'
            dist.barrier()
            feat = y.to(feat.device)
        return y

def evaluate(model, g, dataloader):
    model.eval()
    ys = []
    y_hats = []
    for it, (input_nodes, output_nodes, blocks) in enumerate(dataloader):
        with torch.no_grad():
            x = blocks[0].srcdata['feat']
            ys.append(blocks[-1].dstdata['label'])
            y_hats.append(model(blocks, x))
    return MF.accuracy(torch.cat(y_hats), torch.cat(ys))

def layerwise_infer(proc_id, device, g, nid, model, use_uva, batch_size):
    model.eval()
    with torch.no_grad():
        pred = model.module.inference(g, device, batch_size, use_uva)
        pred = pred[nid]
        labels = g.ndata['label'][nid].to(pred.device)
    if proc_id == 0:
        acc = MF.accuracy(pred, labels)
        print('Test acc:', acc.item())

def train(proc_id, nprocs, device, g, train_idx, val_idx, model, use_uva):
    sampler = NeighborSampler([10, 10, 10],
                              prefetch_node_feats=['feat'],
                              prefetch_labels=['label'])
    train_dataloader = DataLoader(g, train_idx, sampler, device=device,
                                  batch_size=1024, shuffle=True,
                                  drop_last=False, num_workers=0,
                                  use_ddp=True, use_uva=use_uva)
    val_dataloader = DataLoader(g, val_idx, sampler, device=device,
                                batch_size=1024, shuffle=True,
                                drop_last=False, num_workers=0,
                                use_ddp=True, use_uva=use_uva)
    opt = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=5e-4)
    for epoch in range(10):
        model.train()
        total_loss = 0
        for it, (input_nodes, output_nodes, blocks) in enumerate(train_dataloader):
            x = blocks[0].srcdata['feat']
            y = blocks[-1].dstdata['label']
            y_hat = model(blocks, x)
            loss = F.cross_entropy(y_hat, y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += loss
        acc = evaluate(model, g, val_dataloader).to(device) / nprocs
        dist.reduce(acc, 0)
        if (proc_id == 0):
            print("Epoch {:05d} | Loss {:.4f} | Accuracy {:.4f} "
                  .format(epoch, total_loss / (it+1), acc.item()))

def run(proc_id, nprocs, devices, g, dist_data, mode):
    # find corresponding device for my rank
    device = devices[proc_id]
    torch.cuda.set_device(device)
    # initialize process group and unpack data for sub-processes
    dist.init_process_group(backend="nccl", init_method='tcp://127.0.0.1:12345', world_size=nprocs, rank=proc_id)
    out_size, train_idx, val_idx, test_idx = dist_data
    train_idx = train_idx.to(device)
    val_idx = val_idx.to(device)
    g = g.to(device if mode == 'puregpu' else 'cpu')
    # create RGCN model (distributed)
    in_size = g.ndata['feat'].shape[1]
    model = SAGE(in_size, 256, out_size).to(device)
    model = DistributedDataParallel(model, device_ids=[device], output_device=device)
    # training + testing
    use_uva = (mode == 'mixed')
    train(proc_id, nprocs, device, g, train_idx, val_idx, model, use_uva)
    layerwise_infer(proc_id, device, g, test_idx, model, use_uva, batch_size=2**16)    
    # cleanup process group
    dist.destroy_process_group()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default='mixed', choices=['mixed', 'puregpu'],
                        help="Training mode. 'mixed' for CPU-GPU mixed training, "
                             "'puregpu' for pure-GPU training.")
    parser.add_argument("--gpu", type=str, default='0',
                           help="GPU(s) in use. Can be a list of gpu ids for multi-gpu training,"
                                " e.g., 0,1,2,3.")
    args = parser.parse_args()
    devices = list(map(int, args.gpu.split(',')))
    nprocs = len(devices)
    assert torch.cuda.is_available(), f"Must have GPUs to enable multi-gpu training."
    print(f'Training in {args.mode} mode.')
    # load and preprocess dataset
    print('Loading data')
    dataset = AsNodePredDataset(DglNodePropPredDataset('ogbn-products'))
    g = dataset[0]
    # avoid creating certain graph formats in each sub-process to save momory
    g.create_formats_()
    # thread limiting to avoid resource competition
    os.environ['OMP_NUM_THREADS'] = str(mp.cpu_count() // 2 // nprocs)    
    dist_data = dataset.num_classes, dataset.train_idx, dataset.val_idx, dataset.test_idx
    
    mp.spawn(run, args=(nprocs, devices, g, dist_data, args.mode), nprocs=nprocs)
