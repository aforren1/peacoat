import ctypes
import gc
import multiprocessing as mp
import os
from collections import namedtuple
from copy import copy
from sys import platform

import numpy as np
import psutil


def shared_to_numpy(mp_arr, dims, dtype):
    """Convert a :class:`multiprocessing.Array` to a numpy array.
    Helper function to allow use of a :class:`multiprocessing.Array` as a numpy array.
    Derived from the answer at:
    <https://stackoverflow.com/questions/7894791/use-numpy-array-in-shared-memory-for-multiprocessing>
    """
    return np.frombuffer(mp_arr.get_obj(), dtype=dtype).reshape(dims)


class MpDevice(object):
    def __init__(self, device=None, high_priority=True,
                 buffer_len=None, **device_kwargs):
        """
        buffer_len overrides default (1000 samples) and sampling_frequency-based (1s worth)
        """
        self.device = device
        self.device_kwargs = device_kwargs
        self.buffer_len = buffer_len
        self.high_priority = high_priority

    def start(self):
        # For Macs, use spawn (interaction with OpenGL or ??)
        # Windows only does spawn
        if platform == 'darwin' or platform == 'win32':
            try:
                mp.set_start_method('spawn')
            except (AttributeError, RuntimeError):
                pass  # already started a process, ?? for other reason

        n_buffers = 2
        self.shared_locks = []
        # make one lock per buffer
        for i in range(n_buffers):
            self.shared_locks.append(mp.RLock())
        self.remote_ready = mp.Event()  # signal to main process that remote is done setup
        self.kill_remote = mp.Event()  # signal to remote process to die
        self.remote_done = mp.Event()
        self.current_buffer_index = mp.Value(ctypes.c_bool, 0, lock=False)  # shouldn't need a lock

        # figure out number of observations to save between reads
        nrow = 100
        if self.device.sampling_frequency:
            nrow = self.device.sampling_frequency
        # if the user specified a runtime sampling_frequency, use that
        kwarg_freq = self.device_kwargs.get('sampling_frequency', None)
        if kwarg_freq:
            nrow = kwarg_freq
        if self.buffer_len:  # buffer_len overcomes all
            nrow = self.buffer_len
        nrow = max(int(nrow), 1)  # make sure we have at least one row
        _device_obs = self.device.get_obs()
        # use this tuple to return data
        self._return_tuple = self.device.build_named_tuple(_device_obs)
        self.nt = namedtuple('obs', 'time data')
        self._data = [[] for i in range(n_buffers)]  # one set of data per buffer
        # for each observation type, allocate arrays (though note that
        # there's nothing useful in them right now)
        for i in range(n_buffers):
            for obs in _device_obs:
                self._data[i].append(DataGlob(obs.ctype, obs.shape, nrow, self.shared_locks[i]))

        self._res = [None] * len(self._data[0])

        self.process = mp.Process(target=remote,
                                  args=(self.device, self.device_kwargs, self._data,
                                        self.remote_ready, self.kill_remote, os.getpid(),
                                        self.current_buffer_index, self.remote_done))
        self.process.daemon = True
        self.process.start()
        self.ps_process = psutil.Process(self.process.pid)
        self.original_nice = self.ps_process.nice()
        self.set_high_priority(self.high_priority)  # lame try to set priority
        self.remote_ready.wait()  # pause until the remote says it's ready

    # @profile
    def read(self):
        # get the currently used buffer
        # note that the value only changes *if* this function has acquired the
        # lock, so we should always be safe to access w/o lock here
        current_buffer_index = int(self.current_buffer_index.value)
        # this *may* block, if the remote is currently writing
        with self._data[current_buffer_index][0].counter.get_lock():
            for counter, datum in enumerate(self._data[current_buffer_index]):
                datum.local_count = datum.counter.value
                datum.counter.value = 0  # reset (so that we start writing to top of array)
                if datum.local_count > 0:
                    np.copyto(datum.local_data.time, datum.np_data.time)
                    np.copyto(datum.local_data.data, datum.np_data.data)
                    self._res[counter] = self.nt(time=datum.local_data.time[0:datum.local_count],
                                                 data=datum.local_data.data[0:datum.local_count, :])
                else:
                    self._res[counter] = self.nt(None, None)
        return self._return_tuple(*self._res)  # plug values into namedtuple

    def stop(self):
        self.set_high_priority(False)
        self.kill_remote.set()
        self.remote_done.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def set_high_priority(self, val=False):
        try:
            if val:
                if psutil.WINDOWS:
                    self.ps_process.nice(psutil.HIGH_PRIORITY_CLASS)
                else:
                    self.ps_process.nice(-10)
            else:
                self.ps_process.nice(self.original_nice)
        except (psutil.AccessDenied, psutil.NoSuchProcess) as e:
            pass


def remote(device, device_kwargs, shared_data,
           # extras
           remote_ready, kill_remote, parent_pid,
           current_buffer_index, remote_done):

    def process_data():
        shared = shared_data[buffer_index][counter]  # alias
        if shared.counter.value < shared.nrow:  # haven't filled buffer yet, so next available row
            next_index = shared.counter.value
            shared.np_data.time[next_index] = datum.time
            shared.np_data.data[next_index, :] = datum.data
        else:  # rolling buffer, had cursed my bedroom
            shared.np_data.time[:] = np.roll(shared.np_data.time, -1, axis=0)
            shared.np_data.time[-1] = datum.time
            shared.np_data.data[:] = np.roll(shared.np_data.data, -1, axis=0)
            shared.np_data.data[-1, :] = datum.data
        # successful read, increment the indexing counter for this stream of data
        shared.counter.value += 1

    gc.disable()
    dev = device(**device_kwargs)

    with dev:
        remote_ready.set()  # signal to the local process that remote is ready to go
        while not kill_remote.is_set() and psutil.pid_exists(parent_pid):
            data = dev.read()  # get observation(s) from device
            buffer_index = int(current_buffer_index.value)  # can only change later
            if isinstance(data, list):  # if a list of observations, rather than a single one
                flag = any([[d for d in l] for l in data])
                is_list = True
            else:
                flag = any([d for d in data])
                is_list = False
            if flag:  # any data at all (otherwise, don't bother acquiring locks)
                # test whether the current buffer is accessible
                lck = shared_data[buffer_index][0].counter.get_lock()
                success = lck.acquire(block=False)
                if not success:  # switch to the other buffer (the local process is in the midst of reading)
                    current_buffer_index.value = not current_buffer_index.value
                    buffer_index = int(current_buffer_index.value)
                    lck = shared_data[buffer_index][0].counter.get_lock()
                    lck.acquire()
                try:
                    if is_list:
                        for dat in data:
                            for counter, datum in enumerate(dat):
                                if datum:  # if there's an observation for this one (possible to have None)
                                    process_data()
                    else:
                        for counter, datum in enumerate(data):
                            if datum:
                                process_data()
                finally:
                    lck.release()
    remote_done.set()


# make sure this is visible for pickleability
obs = namedtuple('obs', 'time data')


class DataGlob(object):
    def __init__(self, ctype, shape, nrow, lock):
        self.new_dims = (nrow,) + shape
        self.ctype = ctype
        prod = int(np.prod(self.new_dims))
        # don't touch (usually)
        self.nrow = int(nrow)
        self._mp_data = obs(time=mp.Array(ctypes.c_double, self.nrow, lock=lock),
                            data=mp.Array(ctype, prod, lock=lock))
        self.counter = mp.Value(ctypes.c_uint, 0, lock=lock)
        self.generate_np_version()
        self.generate_local_version()

    def generate_np_version(self):
        self.np_data = obs(time=shared_to_numpy(self._mp_data.time, (self.nrow,), ctypes.c_double),
                           data=shared_to_numpy(self._mp_data.data, self.new_dims, self.ctype))

    def generate_local_version(self):
        self.local_data = obs(time=self.np_data.time.copy(),
                              data=self.np_data.data.copy())
        self.local_count = 0


if __name__ == '__main__':
    from time import time, sleep
    from timeit import default_timer
    from mockdevices import Dummy
    import matplotlib.pyplot as plt
    Dummy.sampling_frequency = 1000
    dev = MpDevice(Dummy)
    times = []
    with dev:
        start = time()
        while time() - start < 10:
            t0 = default_timer()
            dat = dev.read()
            t1 = default_timer()
            if dat.num1.data is not None:
                dff = t1 - t0
                # print(dff)
                # print(dat.num1.data.shape)
                # print(np.diff(dat.num1.time))
                times.append(np.diff(dat.num1.time))
                sleep(0.016)
    plt.plot(np.hstack(times))
    plt.show()