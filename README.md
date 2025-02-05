# Multiscale_Neuromod_PyTorch

This repo contains the PyTorch implementation of the paper
"Effects of neuromodulation-inspired mechanisms on the performance of 
deep neural networks in a spatial learning task"
and is inspired by the base model in 
Banino et al.'s https://doi.org/10.1038/s41586-018-0102-6.

All hyperparameters can be modified in main.py. To run and train the model simply use
```
python main.py
```

To evaluate, first set the checkpoint path and hyperparameters in eval.py then run:
```
python eval.py
```