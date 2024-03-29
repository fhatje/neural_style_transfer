from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.transforms as transforms
import torchvision.models as models
from pathlib import Path
import copy
import fire
from PIL import Image
import matplotlib.pyplot as plt
import os

# Inputs

style_path = r"./data/images/neural_style/absku_01.jpg"
content_path = r"./data/images/neural_style/hk_01.jpg"
output_dir = r"hk_absku_01"
save_every = 25
epochs = 10000
img_res = 1024
random_start = False
warm_up = True
warm_up_runs = 1000
if warm_up:
    assert warm_up_runs < epochs
losses = {"style": [], "content": []}

content_layers_default = ['conv_4']
style_layers_default = ['conv_1', 'conv_2', 'conv_3', 'conv_4', 'conv_5'] 


class ContentLoss(nn.Module):

    def __init__(self, target,):
        super(ContentLoss, self).__init__()
        # we 'detach' the target content from the tree used
        # to dynamically compute the gradient: this is a stated value,
        # not a variable. Otherwise the forward method of the criterion
        # will throw an error.
        self.target = target.detach()

    def forward(self, ipt):
        self.loss = F.mse_loss(ipt, self.target)
        return ipt


class StyleLoss(nn.Module):

    def __init__(self, target_feature):
        super(StyleLoss, self).__init__()
        self.target = gram_matrix(target_feature).detach()

    def forward(self, ipt):
        G = gram_matrix(ipt)
        self.loss = F.mse_loss(G, self.target)
        return ipt

    


# create a module to normalize input image so we can easily put it in a
# nn.Sequential
class Normalization(nn.Module):
    def __init__(self, mean, std):
        super(Normalization, self).__init__()
        # .view the mean and std to make them [C x 1 x 1] so that they can
        # directly work with image Tensor of shape [B x C x H x W].
        # B is batch size. C is number of channels. H is height and W is width.
        self.mean = torch.tensor(mean).view(-1, 1, 1)
        self.std = torch.tensor(std).view(-1, 1, 1)

    def forward(self, img):
        # normalize img
        return (img - self.mean) / self.std

def gram_matrix(ipt):
        a, b, c, d = ipt.size()  # a=batch size(=1)
        # b=number of feature maps
        # (c,d)=dimensions of a f. map (N=c*d)

        features = ipt.view(a * b, c * d)  # resise F_XL into \hat F_XL

        G = torch.mm(features, features.t())  # compute the gram product

        # we 'normalize' the values of the gram matrix
        # by dividing by the number of element in each feature maps.
        return G.div(a * b * c * d)

def generate_image(
                    content_img=None,    # Remove None
                    style_img=None,      # Remove None
                    output_dir="output",
                    output_name="output_img.jpg",
                    epochs=1000, 
                    img_res=1024,
                    random_start=False):

    global content_layers_default, style_layers_default

    # Functions
    def image_loader(image_name):
        #globals loader
        image = Image.open(image_name)
        # fake batch dimension required to fit network's input dimensions
        image = loader(image).unsqueeze(0)
        return image.to(device, torch.float)

    def imshow(tensor, title=None):
        image = tensor.cpu().clone()  # we clone the tensor to not do changes on it
        image = image.squeeze(0)      # remove the fake batch dimension
        image = unloader(image)
        plt.imshow(image)
        if title is not None:
            plt.title(title)
        plt.pause(0.001) # pause a bit so that plots are updated

    def get_input_optimizer(input_img):
        # this line to show that input is a parameter that requires a gradient
        optimizer = optim.LBFGS([input_img.requires_grad_()])
        return optimizer

    def imsave(tensor, output_dir="output", title="output_img.jpg"):
        #global output_dir
        image = tensor.cpu().clone()  # we clone the tensor to not do changes on it
        image = image.squeeze(0)      # remove the fake batch dimension
        image = unloader(image)
        path = Path(output_dir)
        #image.save(f"output/{output_dir}/img_E_{epoch}.jpg")
        image.save(path/title)

    def get_style_model_and_losses(cnn, normalization_mean, normalization_std,
                               style_img, content_img,
                               content_layers=content_layers_default,
                               style_layers=style_layers_default):
        cnn = copy.deepcopy(cnn)

        # normalization module
        normalization = Normalization(normalization_mean, normalization_std).to(device)

        # just in order to have an iterable access to or list of content/syle
        # losses
        content_losses = []
        style_losses = []

        # assuming that cnn is a nn.Sequential, so we make a new nn.Sequential
        # to put in modules that are supposed to be activated sequentially
        model = nn.Sequential(normalization)

        i = 0  # increment every time we see a conv
        for layer in cnn.children():
            if isinstance(layer, nn.Conv2d):
                i += 1
                name = 'conv_{}'.format(i)
            elif isinstance(layer, nn.ReLU):
                name = 'relu_{}'.format(i)
                # The in-place version doesn't play very nicely with the ContentLoss
                # and StyleLoss we insert below. So we replace with out-of-place
                # ones here.
                layer = nn.ReLU(inplace=False)
            elif isinstance(layer, nn.MaxPool2d):
                name = 'pool_{}'.format(i)
            elif isinstance(layer, nn.BatchNorm2d):
                name = 'bn_{}'.format(i)
            else:
                raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

            model.add_module(name, layer)

            if name in content_layers:
                # add content loss:
                target = model(content_img).detach()
                content_loss = ContentLoss(target)
                model.add_module("content_loss_{}".format(i), content_loss)
                content_losses.append(content_loss)

            if name in style_layers:
                # add style loss:
                target_feature = model(style_img).detach()
                style_loss = StyleLoss(target_feature)
                model.add_module("style_loss_{}".format(i), style_loss)
                style_losses.append(style_loss)

        # now we trim off the layers after the last content and style losses
        for i in range(len(model) - 1, -1, -1):
            if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
                break

        model = model[:(i + 1)]

        return model, style_losses, content_losses

    def run_style_transfer(cnn, normalization_mean, normalization_std,
                       content_img, style_img, input_img, num_steps=2000,
                       style_weight=1000000, content_weight=1):
        """Run the style transfer."""
        print('Building the style transfer model..')
        model, style_losses, content_losses = get_style_model_and_losses(cnn,
            normalization_mean, normalization_std, style_img, content_img)
        optimizer = get_input_optimizer(input_img)

        print('Optimizing..')
        run = [0]
        while run[0] <= num_steps:

            def closure():
                global save_every, losses, warm_up, warm_up_runs
                # correct the values of updated input image
                input_img.data.clamp_(0, 1)

                optimizer.zero_grad()
                model(input_img)
                style_score = 0
                content_score = 0

                for sl in style_losses:
                    style_score += sl.loss
                for cl in content_losses:
                    content_score += cl.loss

                style_score *= style_weight
                content_score *= content_weight

                loss = style_score + content_score
                loss.backward()
                
                # Keep track of losses over time
                losses["style"].append(style_score.item())
                losses["content"].append(content_score.item())
                
                # Auto Stop if style loss explodes
                #print(f"Warm-Up: {warm_up}")

                run[0] += 1
                
                if run[0] % save_every == 10:
                    print(f"Run {run} completed!")
                    
                return style_score + content_score

            optimizer.step(closure)

        # a last correction...
        input_img.data.clamp_(0, 1)

        return input_img


    # Check if output path already exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    #print(output_dir)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


    loader = transforms.Compose([
    transforms.Resize(img_res),  # scale imported image
    transforms.ToTensor()])  # transform it into a torch tensor

    style_img = image_loader(style_img)
    content_img = image_loader(content_img)

    assert style_img.size() == content_img.size(), \
    "we need to import style and content images of the same size"

    unloader = transforms.ToPILImage()  # reconvert into PIL image

    #plt.ion()
    #plt.figure()
    #imshow(style_img, title='Style Image')

    cnn = models.vgg19(pretrained=True).features.to(device).eval()
    cnn_normalization_mean = torch.tensor([0.485, 0.456, 0.406]).to(device)
    cnn_normalization_std = torch.tensor([0.229, 0.224, 0.225]).to(device)

     

    input_img = content_img.clone()
    if random_start:
        input_img = torch.randn(content_img.data.size(), device=device)

    # add the original input image to the figure:
    #plt.figure()
    #imshow(input_img, title='Input Image')

    output = run_style_transfer(cnn, cnn_normalization_mean, cnn_normalization_std,
                            content_img, style_img, input_img, num_steps=epochs)

    imsave(output.data.clamp_(0, 1), output_dir, output_name)




if __name__ == "__main__":
    #generate_image(None, None, output_dir="GOIL")
    fire.Fire(generate_image)
    
    
    print("ENDE")