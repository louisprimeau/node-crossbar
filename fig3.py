import torch
from torchvision import transforms, datasets
import torch.nn as nn
import torch.nn.functional as F
import sim.networks.Conv as conv
import sim.networks.Linear as linear
import sim.networks.rk_net as rk_net
import sim.crossbar.crossbar as crossbar

import seaborn as sns
import matplotlib.pyplot as plt
import matplotlib

transform = transforms.Compose([transforms.Resize((8,8)),
                                transforms.ToTensor(), 
                                transforms.Normalize((0.5,), (0.5,)),
])

trainset = datasets.MNIST('~/mnist/', download=False, train=True, transform=transform)
valset = datasets.MNIST('~/mnist/', download=False, train=False, transform=transform)
trainloader = torch.utils.data.DataLoader(trainset, batch_size=64, shuffle=True)
valloader = torch.utils.data.DataLoader(valset, batch_size=64, shuffle=True)


device_params = {"Vdd": 1.8,
                 "r_wl": 20,
                 "r_bl": 20,
                 "m": 128,
                 "n": 128,
                 "r_on": 1e4,
                 "r_off": 1e5,
                 "dac_resolution": 4,
                 "adc_resolution": 14,
                 "bias_scheme": 1/3,
                 "tile_rows": 8,
                 "tile_cols": 8,
                 "r_cmos_line": 600,
                 "r_cmos_transistor": 20,
                 "r_on_stddev": 1e3,
                 "r_off_stddev": 1e4,
                 "p_stuck_on": 0.01,
                 "p_stuck_off": 0.01,
                 "method": "viability",
                 "viability": 0.05,
}

class ode_net(torch.nn.Module):
    def __init__(self):
        super(ode_net, self).__init__()

        self.cb = crossbar.crossbar(device_params, deterministic=False)
        self.conv1 = conv.Conv2d(1, 3, 3, self.cb, stride=2, padding=1)
        self.ode_block = rk_net.RK_net(3, self.cb)
        self.linear = linear.Linear(4 * 4 * 3, 40, self.cb)
        self.linear2 = linear.Linear(40, 10, self.cb)
        
    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = self.ode_block(x)
        x = F.relu(self.linear(torch.flatten(x, 1).unsqueeze(2)).squeeze(2))
        x = self.linear2(torch.flatten(x, 1).unsqueeze(2)).squeeze(2)

        return x
    
    def remap(self):
        self.conv1.remap()
        self.ode_block.remap()

    def use_cb(self, state):
        self.conv1.use_cb(state)
        self.ode_block.use_cb(state)
        self.linear.use_cb(state)
        self.linear2.use_cb(state)

class regular_net(torch.nn.Module):
    def __init__(self):
        super(regular_net, self).__init__()

        self.cb = crossbar.crossbar(device_params, deterministic=False)
        self.conv1 = conv.Conv2d(1, 3, 3, self.cb, stride=2, padding=1)
        self.resnet = torch.nn.Sequential(conv.Conv2d(3, 3, 3, self.cb, padding=1),
                                          torch.nn.ReLU(),
                                          conv.Conv2d(3, 3, 3, self.cb, padding=1),
                                         )
        
        self.linear = linear.Linear(4 * 4 * 3, 40, self.cb)
        self.linear2 = linear.Linear(40, 10, self.cb)
    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = torch.nn.functional.relu(self.resnet(x) + x)
        x = F.relu(self.linear(torch.flatten(x, 1).unsqueeze(2)).squeeze(2))
        x = self.linear2(torch.flatten(x, 1).unsqueeze(2)).squeeze(2)
        return x

    def remap(self):
        return None

    def use_cb(self, state):
        self.conv1.use_cb(state)
        self.resnet[0].use_cb(state)
        self.resnet[2].use_cb(state)
        self.linear.use_cb(state)
        self.linear2.use_cb(state)

def train(network, epochs):

    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(network.parameters(), lr=0.001, momentum=0.9)

    losses, accuracies = [], []
    for epoch in range(1, epochs+1):
        print("Epoch", epoch)
        num_correct = 0
        epoch_loss, num_batches = 0, 0

        with torch.enable_grad():
        
            for i, (example, label) in enumerate(trainloader, 0):
                network.zero_grad()
                #if i == 100: break

                out = network(example) 
                loss = criterion(out, label)
                loss.backward()

                #print(network.ode_ef_block.kernel2.grad)
                #print(network.ode_ef_block.conv1.conv_weight.grad)

                network.remap()
                optimizer.step()

                epoch_loss += loss
                num_batches += 1
                num_correct += torch.sum(torch.argmax(out, 1) == label)

            
        losses.append((epoch_loss / num_batches).detach())
        print("Train Score: {:.2f}%, ({} / {})".format(num_correct / i / 64 * 100, num_correct, i*64))

        with torch.no_grad():
        
            num_correct = 0
            for example, label in valloader:
                out = torch.argmax(network(example), 1)
                num_correct += torch.sum(out == label)

        print("Validation Score: {:.2f}%, ({} / {})".format(num_correct / len(valloader) / 64 * 100, num_correct, len(valloader)*64))
        accuracies.append(num_correct / len(valloader) / 64)
        
    return network, losses, accuracies

model = ode_net()


uc = 1e3
weights = [(1 / model.cb.W[coord[0]:coord[0]+coord[2], coord[1]*2:coord[1]*2+coord[3]*2]) * (1/uc) for coord in model.cb.mapped] + [(1 / model.cb.W) * (1/uc)]
vmax = max(torch.max(weight) for weight in weights)
vmin = min(torch.min(weight) for weight in weights)

print(vmax, vmin)

matplotlib.rcParams.update({'font.size': 18})
fig5, ax_cmap = plt.subplots(ncols=len(weights), figsize=(20, 3))
cmap = sns.blend_palette(("#fa7de3", "#ffffff", "#6ef3ff"), n_colors=9, as_cmap=True, input='hex')

for ax in ax_cmap:
    ax.set(xticklabels=[])
    ax.set(yticklabels=[])


for i, weight in enumerate(weights):
    sns.heatmap(weight.detach(), vmax=vmax, vmin=vmin, cmap=cmap, square=True, cbar=False if i!=len(weights)-1 else True, ax=ax_cmap[i])


fig6, ax_cmap2 = plt.subplots(figsize=(16, 20))

ax_cmap2.set(xticklabels=[])
ax_cmap2.set(yticklabels=[])

sns.heatmap(weights[-1].detach(), vmax=vmax, vmin=vmin, cmap=cmap, square=True, cbar=True, ax=ax_cmap2)


# Network Model Comparison

test_network_1 = ode_net()
test_network_2 = regular_net()

# Training

test_network_1.use_cb(False)
test_network_2.use_cb(False)

epochs = 30
test_network_1, losses_1, accuracies_1 = train(test_network_1, epochs)
test_network_2, losses_2, accuracies_2 = train(test_network_2, epochs)

# Validation

out1, out2, total = 0.0, 0.0, 0
for item in valloader:
    out1 += torch.sum(torch.argmax(test_network_1(item[0]), 1) == item[1])
    out2 += torch.sum(torch.argmax(test_network_2(item[0]), 1) == item[1])
    total += item[0].size(0)
print("ODE Net Performance on Val Set: {:.2f}".format(out1 / total), "Conventional Performance on Val Set: {:.2f}".format(out2 / total), sep="\n")


fig1, ax1 = plt.subplots(nrows=1)

ax1.plot(list(range(epochs)),
         [a.detach().item() for a in losses_1],     
         'o-',
         linewidth=0.5,
         color='deepskyblue',
         markerfacecolor='none',
         )

ax1.plot(list(range(epochs)),
         [a.detach().item() for a in losses_2],     
         'o-',
         linewidth=0.5,
         color='crimson',
         markerfacecolor='none',
         )

ax1.spines['top'].set_visible(False)
ax1.set_xlabel('Epoch', fontsize=16)
ax1.set_ylabel('Cross Entropy Loss', fontsize=16)
ax1.legend(('NODE', 'Conventional'), loc='upper right')


fig2, ax2 = plt.subplots(nrows=1)

ax2.plot(list(range(epochs)),
         accuracies_1,
         'o-',
         linewidth=0.5,
         color='deepskyblue',
         markerfacecolor='none',
         )

ax2.plot(list(range(epochs)),
         accuracies_2,
         'o-',
         linewidth=0.5,
         color='crimson',
         markerfacecolor='none',
         )

ax2.spines['top'].set_visible(False)
ax2.set_xlabel('Epoch', fontsize=16)
ax2.set_ylabel('Validation Accuracy', fontsize=16)
ax2.legend(('NODE', 'Conventional'), loc='upper right')


fig1.savefig('output/fig3/fig1.png', transparent=True)
fig2.savefig('output/fig3/fig2.png', transparent=True)
fig5.savefig('output/fig3/fig5.png', transparent=True)
fig6.savefig('output/fig3/fig6.png', transparent=True)



plt.show()
