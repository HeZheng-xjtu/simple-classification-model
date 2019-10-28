from __future__ import print_function
from __future__ import division

import argparse
import random
import torch
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch import nn
from torch.autograd import Variable
import numpy as np
import os
import utils
import dataset
import torchvision.models as models

parser = argparse.ArgumentParser()
parser.add_argument('--trainRoot', help='path to dataset', default='F:\\data')
parser.add_argument('--valRoot', help='path to dataset', default='F:\\valdata')
parser.add_argument('--workers',
                    type=int,
                    help='number of data loading workers',
                    default=0)
parser.add_argument('--batchSize',
                    type=int,
                    default=6,
                    help='input batch size')
parser.add_argument('--imgH',
                    type=int,
                    default=300,
                    help='the height of the input image to network')
parser.add_argument('--imgW',
                    type=int,
                    default=300,
                    help='the width of the input image to network')
parser.add_argument('--nepoch',
                    type=int,
                    default=10,
                    help='number of epochs to train for')
# TODO(meijieru): epoch -> iter
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--pretrained',
                    default='',
                    help="path to pretrained model (to continue training)")
parser.add_argument('--expr_dir',
                    default='expr',
                    help='Where to store samples and models')
parser.add_argument('--displayInterval',
                    type=int,
                    default=500,
                    help='Interval to be displayed')
parser.add_argument('--n_test_disp',
                    type=int,
                    default=500,
                    help='Number of samples to display when test')
parser.add_argument('--valInterval',
                    type=int,
                    default=500,
                    help='Interval to be displayed')
parser.add_argument('--saveInterval',
                    type=int,
                    default=500,
                    help='Interval to be displayed')
parser.add_argument('--lr',
                    type=float,
                    default=0.001,
                    help='learning rate for Critic, not used by adadealta')
parser.add_argument('--beta1',
                    type=float,
                    default=0.5,
                    help='beta1 for adam. default=0.5')
parser.add_argument('--adam',
                    action='store_true',
                    help='Whether to use adam (default is rmsprop)')
parser.add_argument('--adadelta',
                    action='store_true',
                    help='Whether to use adadelta (default is rmsprop)')
parser.add_argument('--keep_ratio',
                    action='store_true',
                    help='whether to keep ratio for image resize')
parser.add_argument('--manualSeed',
                    type=int,
                    default=1234,
                    help='reproduce experiemnt')
parser.add_argument('--random_sample',
                    action='store_true',
                    default=True,
                    help='whether to sample the dataset with random sampler')
opt = parser.parse_args()
print(opt)

if not os.path.exists(opt.expr_dir):
    os.makedirs(opt.expr_dir)

random.seed(opt.manualSeed)
np.random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)

cudnn.benchmark = True

if torch.cuda.is_available() and not opt.cuda:
    print(
        "WARNING: You have a CUDA device, so you should probably run with --cuda"
    )

train_dataset = dataset.lmdbDataset(root=opt.trainRoot, aug=True)
assert train_dataset
if not opt.random_sample:
    sampler = dataset.randomSequentialSampler(train_dataset, opt.batchSize)
else:
    sampler = None
train_loader = torch.utils.data.DataLoader(train_dataset,
                                           batch_size=opt.batchSize,
                                           shuffle=True,
                                           sampler=sampler,
                                           num_workers=int(opt.workers),
                                           collate_fn=dataset.alignCollate(
                                               imgH=opt.imgH,
                                               imgW=opt.imgW,
                                               keep_ratio=opt.keep_ratio))
test_dataset = dataset.lmdbDataset(root=opt.valRoot,
                                   transform=dataset.resizeNormalize(
                                       (300, 300)),
                                   aug=False)

criterion = nn.CrossEntropyLoss()


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


model = models.resnet50(num_classes=2)
model.apply(weights_init)
if opt.pretrained != '':
    print('loading pretrained model from %s' % opt.pretrained)
    model.load_state_dict(torch.load(opt.pretrained))
print(model)

image = torch.FloatTensor(opt.batchSize, 3, opt.imgH, opt.imgW)
label = torch.FloatTensor(opt.batchSize, 1)

if opt.cuda:
    model.cuda()
    image = image.cuda()
    label = label.cuda()
    criterion = criterion.cuda()
    model = nn.DataParallel(model)

image = Variable(image)
label = Variable(label)

# loss averager
loss_avg = utils.averager()

# setup optimizer
if opt.adam:
    optimizer = optim.Adam(model.parameters(),
                           lr=opt.lr,
                           betas=(opt.beta1, 0.999))
elif opt.adadelta:
    optimizer = optim.Adadelta(model.parameters())
else:
    optimizer = optim.RMSprop(model.parameters(), lr=opt.lr)


def val(net, dataset, criterion, max_iter=100):
    print('Start val')

    for p in model.parameters():
        p.requires_grad = False

    net.eval()
    data_loader = torch.utils.data.DataLoader(dataset,
                                              shuffle=True,
                                              batch_size=opt.batchSize,
                                              num_workers=int(opt.workers))
    val_iter = iter(data_loader)

    i = 0
    n_correct = 0
    loss_avg = utils.averager()

    max_iter = min(max_iter, len(data_loader))
    for i in range(max_iter):
        data = val_iter.next()
        i += 1
        cpu_images, cpu_labels = data
        batch_size = cpu_images.size(0)
        utils.loadData(image, cpu_images)
        utils.loadData(label, cpu_labels)

        preds = model(image)

        cost = criterion(preds, label.long())
        loss_avg.add(cost)

        _, predicted = torch.max(preds, 1)
        for pre, tar in zip(predicted, cpu_labels):
            if pre.cpu() == tar:
                n_correct += 1

    # 下面这个计算方式应该有点问题，因为batch_size不是每一次都是满的，所以这样除肯定会除多
    accuracy = n_correct / float(max_iter * opt.batchSize)
    print('Test loss: %f, accuray: %f' % (loss_avg.val(), accuracy))


def trainBatch(net, criterion, optimizer):
    data = train_iter.next()
    cpu_images, cpu_labels = data

    batch_size = cpu_images.size(0)
    utils.loadData(image, cpu_images)
    utils.loadData(label, cpu_labels)

    preds = model(image)

    cost = criterion(preds, label.long())
    model.zero_grad()
    cost.backward()
    optimizer.step()
    return cost


for epoch in range(opt.nepoch):
    train_iter = iter(train_loader)
    i = 0

    while i < len(train_loader):
        for p in model.parameters():
            p.requires_grad = True
        model.train()

        cost = trainBatch(model, criterion, optimizer)
        loss_avg.add(cost)
        i += 1

        if i % opt.displayInterval == 0:
            print('[%d/%d][%d/%d] Loss: %f' %
                  (epoch, opt.nepoch, i, len(train_loader), loss_avg.val()))
            loss_avg.reset()

        if i % opt.valInterval == 0:
            #pass
            val(model, test_dataset, criterion)

        # do checkpointing
        if i % opt.saveInterval == 0 and epoch > 3:
            torch.save(model.state_dict(),
                       '{0}/netRES_{1}_{2}.pth'.format(opt.expr_dir, epoch, i))
