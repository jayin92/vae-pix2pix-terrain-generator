import torch
from .base_model import BaseModel
from . import networks
import random
import numpy as np

class VAEPix2PixModel(BaseModel):
    """ This class implements the pix2pix model, for learning a mapping from input images to output images given paired data.

    The model training requires '--dataset_mode aligned' dataset.
    By default, it uses a '--netG unet256' U-Net generator,
    a '--netD basic' discriminator (PatchGAN),
    and a '--gan_mode' vanilla GAN loss (the cross-entropy objective used in the orignal GAN paper).

    pix2pix paper: https://arxiv.org/pdf/1611.07004.pdf
    """
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        """Add new dataset-specific options, and rewrite default values for existing options.

        Parameters:
            parser          -- original option parser
            is_train (bool) -- whether training phase or test phase. You can use this flag to add training-specific or test-specific options.

        Returns:
            the modified parser.

        For pix2pix, we do not use image buffer
        The training objective is: GAN Loss + lambda_L1 * ||G(A)-B||_1
        By default, we use vanilla GAN loss, UNet with batchnorm, and aligned datasets.
        """
        # changing the default values to match the pix2pix paper (https://phillipi.github.io/pix2pix/)
        parser.set_defaults(norm='batch', netG='unet_256', dataset_mode='aligned')
        if is_train:
            parser.set_defaults(pool_size=0, gan_mode='vanilla')
            parser.add_argument('--lambda_L1', type=float, default=100.0, help='weight for L1 loss')

        return parser

    def __init__(self, opt):
        """Initialize the pix2pix class.

        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        BaseModel.__init__(self, opt)
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['G_GAN', 'G_L1', 'D_real', 'D_fake','KLD','miu','var']
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        self.visual_names = ['real_A', 'fake_B', 'real_B']
        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>
        if self.isTrain:
            self.model_names = ['S','G', 'D']
        else:  # during test time, only load G
            self.model_names = ['S','G']
        # define networks (both generator and discriminator)
        latent_dim = 8
        self.beta = 0.001
        self.epoch=0# used to schedule beta 
        
        
        self.netS = networks.MyDataParallel(networks.VAEEncoder2(opt.input_nc+opt.output_nc,latent_dim,[32,64,128,256,256,256,8,8]),self.gpu_ids)if opt.useVAE2 else networks.MyDataParallel(networks.VAEEncoder(opt.input_nc+opt.output_nc,latent_dim,[32,64,128,256,256,256,8,8]),self.gpu_ids)
        
        self.netG = networks.define_G(opt.input_nc+latent_dim, opt.output_nc, opt.ngf, opt.netG, opt.norm,not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids,att=opt.attention,multsc=opt.mult_skip_conn)
                                  

        if self.isTrain:  # define a discriminator; conditional GANs need to take both input and output images; Therefore, #channels for D is input_nc + output_nc
            self.netD = networks.define_D(opt.input_nc + opt.output_nc + latent_dim, opt.ndf, opt.netD,
                                          opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionL1 = torch.nn.L1Loss()
            self.criterionL2 = torch.nn.MSELoss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            self.optimizer_S = torch.optim.Adam(self.netS.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_G = torch.optim.Adam(self.netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer_S)
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)
            self.epoch=0
    
    def set_input(self, input,epoch=0):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap images in domain A and domain B.
        """
        self.epoch=epoch
        AtoB = self.opt.direction == 'AtoB'
        self.real_A = input['A' if AtoB else 'B'].to(self.device)
        self.real_B = input['B' if AtoB else 'A'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']
        if self.opt.isTrain:    
            if random.randint(0,1):
                self.real_A=torch.flip(self.real_A,[2])
                self.real_B=torch.flip(self.real_B,[2])
            if random.randint(0,1):
                self.real_A=torch.flip(self.real_A,[3])
                self.real_B=torch.flip(self.real_B,[3])
            if random.randint(0,1):
                self.real_A=self.real_A.transpose(2,3)
                self.real_B=self.real_B.transpose(2,3)    
    def test(self):
        self.visual_names = ['real_A', 'fake_B', 'real_B','rand_lat']
        self.netS.eval()
        self.netG.eval()
        self.style = self.netS(torch.cat([self.real_A, self.real_B],dim=1)) 
        self.fake_B = self.netG(torch.cat([self.real_A, self.style],dim=1))

        self.style = torch.randn_like(self.style[:,:,0:1,0:1]).repeat(1,1,256,256)
        self.rand_lat = self.netG(torch.cat([self.real_A, self.style],dim=1))
            
            
    def forward(self, latent=None):
        rand_bias=np.random.uniform(-2,2)
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        if latent == None:
            self.style = self.netS(torch.cat([self.real_A+rand_bias if self.netG.module.output_nc==3 else self.real_A, self.real_B],dim=1)) #+ torch.tensor((np.random.rand(1, 16, 1, 1) * 2 - 1) / 10, dtype=torch.float).to("cuda:0")
        else:
            self.style = latent
        self.fake_B = self.netG(torch.cat([self.real_A+rand_bias if self.netG.module.output_nc==3 else self.real_A, self.style],dim=1))#random bias
    def backward_D(self):
        """Calculate GAN loss for the discriminator"""
        # Fake; stop backprop to the generator by detaching fake_B
        fake_AB = torch.cat([self.real_A, self.fake_B,self.style], dim=1)  # we use conditional GANs; we need to feed both input and output to the discriminator
        pred_fake = self.netD(fake_AB.detach())
        self.loss_D_fake = self.criterionGAN(pred_fake, False)
        # Real
        real_AB = torch.cat([self.real_A, self.real_B,self.style.detach()], dim=1)
        pred_real = self.netD(real_AB)
        self.loss_D_real = self.criterionGAN(pred_real, True)
        # combine loss and calculate gradients
        self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5
        self.loss_D.backward()
    
    def backward_G(self):
        """Calculate GAN and L1 loss for the generator"""
        # First, G(A) should fake the discriminator
        fake_AB = torch.cat((self.real_A, self.fake_B,self.style), 1)
        pred_fake = self.netD(fake_AB)
        self.loss_G_GAN = self.criterionGAN(pred_fake, True)
        # Second, G(A) = B
        self.loss_G_L1 = self.criterionL1(self.fake_B, self.real_B) * self.opt.lambda_L1
        self.loss_KLD = self.netS.loss(self.beta*(0.001+(self.epoch%50)/50.0))
        self.loss_miu=self.netS.rmsMiu()
        self.loss_var=self.netS.meanVar()
        # combine loss and calculate gradients
        self.loss_G = self.loss_G_GAN + self.loss_G_L1 + self.loss_KLD
        self.loss_G.backward()

    def optimize_parameters(self):
        self.forward()                   # compute fake images: G(A)
        # update D
        self.set_requires_grad(self.netD, True)  # enable backprop for D
        self.optimizer_D.zero_grad()     # set D's gradients to zero
        self.backward_D()                # calculate gradients for D
        self.optimizer_D.step()          # update D's weights
        # update G
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G
        self.optimizer_S.zero_grad()
        self.optimizer_G.zero_grad()        # set G's gradients to zero
        self.backward_G()                   # calculate graidents for G
        self.optimizer_S.step()
        self.optimizer_G.step()            # udpate G's weights
    def l2(self,c):
        self.forward() 
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G
        self.optimizer_S.zero_grad()
        self.optimizer_G.zero_grad()        # set G's gradients to zero
        self.loss_G_L2=self.criterionL2(self.fake_B, self.real_A)*c
        self.loss_G_L2.backward()
        self.optimizer_S.step()
        self.optimizer_G.step()