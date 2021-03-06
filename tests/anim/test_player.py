import pytest
from collections import namedtuple
from toon.anim.player import Player
from toon.anim.track import Track
from toon.anim.interpolators import SELECT

from pytest import approx

class Circ(object):
    def __init__(self):
        self.x = 0
        self.y = 0


def test_player():
    trk = Track([(0, 0), (0.5, 0.5), (1, 1)])
    circ = Circ()
    circ2 = Circ()

    def change_val(val, obj):
        obj.x = val

    player = Player()
    # directly access an attribute
    player.add(trk, 'x', circ)
    # callback
    player.add(trk, change_val, circ2)

    player.start(0)
    assert(player.is_playing)
    player.advance(0.5)
    assert(circ.x == circ2.x)
    player.advance(0.9)
    assert(circ.x == circ2.x)
    player.advance(1.0)
    # test if stops after track exhausted
    assert(not player.is_playing)
    # modifying a group of objects (with matching API)
    circs = [Circ() for i in range(5)]

    player.add(trk, 'y', circs)
    player.start(0)
    player.advance(0.5)
    assert(all([i.y == 0.5 for i in circs]))

    player.stop()
    assert(not player.is_playing)

    def call(val, obj, foo):
        obj.x = val * foo

    player.add(trk, call, circ, foo=2)
    player.start(0)
    player.advance(0.5)
    assert(circ.x == 1)


def test_scaling():
    circ = Circ()
    player = Player()
    player.timescale = 0.5
    player.add(Track([(0, 0), (1, 1)]), 'x', circ)
    player.start(0)
    player.advance(0.5)
    assert(circ.x == 0.25)

    player.timescale = 2
    player.start(10)
    player.advance(10.5)
    assert(circ.x == 1)


def test_reset():
    circ = Circ()
    player = Player()
    player.add(Track([(0, 0), (1, 1)]), 'x', circ)
    player.start(0)
    player.advance(0.5)
    player.reset()
    player.advance(0.5)
    assert(circ.x == 0)

def test_repeat():
    circ = Circ()
    player = Player(repeats=2)
    player.add(Track([(0, 0), (1, 1)]), 'x', circ)
    player.start(0)
    player.advance(1.5)
    assert(circ.x == 0.5)

def test_foo():
    circ = Circ()
    player = Player()
    kfs = [(0, 0), (1, 1), (2, -1)]
    player.add(Track(kfs), 'x', circ)
    player.add(Track(kfs, interpolator=SELECT), 'y', circ)
    player.start(0)
    player.advance(0.5)
    assert(circ.x == approx(0.5))
    assert(circ.y == 0)

    player.advance(0.999)
    assert(circ.x == approx(0.999))
    assert(circ.y == 0)
    player.advance(1.0)
    assert(circ.x == circ.y == approx(1.0))

    player.advance(1.5)
    assert(circ.x == approx(0))
    assert(circ.y == approx(1.0))

    player.advance(0.5) # rewind
    assert(circ.x == approx(0.5))
    assert(circ.y == 0)
