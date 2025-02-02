# suppress tensorflow warnings
import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
from warnings import simplefilter # disable tensorflow warnings
simplefilter(action='ignore', category=FutureWarning)

try:
    from robonet.robonet.video_prediction.training import GIFLogger
    from robonet.robonet.video_prediction.training import get_trainable as vpred_trainable
    from robonet.robonet.inverse_model.training import get_trainable as inverse_trainable
except:
    print('could not import trainables!')


def get_trainable(class_name):
    available_trainables = [vpred_trainable, inverse_trainable]
    for a in available_trainables:
        try:
            return a(class_name)
        except NotImplementedError:
            pass
    raise NotImplementedError
