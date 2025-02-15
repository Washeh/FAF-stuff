import transaction
import time
import persistent
import persistent.list
import heapq
import threading
from modules.callbackitem import CallbackItem
from modules.utils import get_logger, get_lock

logger = get_logger('callbackqueue')
lock = get_lock('callbackqueue')  # necessary external lock...


class CallbackQueue(persistent.Persistent):
    """
    The queue containing the CallbackItems.
    Has to be wrapped by a CallbackQueueWorkerThread that works it, due to pickle limitations.
    """

    def __init__(self):
        super(CallbackQueue, self).__init__()
        self.items = []
        logger.info('Created new CallbackQueue')

    def reset(self):
        with lock:
            self.items.clear()
            self.save()
            logger.info('Reset CallbackQueue')

    def migrate(self):
        """ to migrate the db when new class elements are added - call self.save() if you do """
        with lock:
            # self.x = self.__dict__.get('x', 'oh a new self.x!')
            pass

    def print(self):
        with lock:
            logger.info('Callbackqueue has %i queued items' % len(self.items))

    def add(self, item: CallbackItem):
        with lock:
            heapq.heappush(self.items, item)
            self.save()
            logger.debug('Callbackqueue, added to queue, %d, %s'
                         % (len(self.items), ['%3.0f' % (i.time-time.time()) for i in self.items]))

    def pop(self):
        with lock:
            if len(self.items) == 0:
                return None
            item = heapq.heappop(self.items)
            self.save()
            transaction.commit()
            logger.debug('Callbackqueue, removed an item from queue, %d remaining' % len(self.items))
            return item

    def should_pop(self):
        with lock:
            if len(self.items) == 0:
                return False
            return self.items[0].ended()

    def save(self):
        with lock:
            transaction.begin()
            self._p_changed = True
            transaction.commit()


class CallbackQueueWorkerThread(threading.Thread):
    def __init__(self, queue: CallbackQueue):
        super(CallbackQueueWorkerThread, self).__init__()
        self.keep_running = True
        self.daemon = True
        self.queue = queue
        logger.info('Created new CallbackQueueWorkerThread')

    def stop(self):
        self.keep_running = False

    def run(self):
        while self.keep_running:
            while self.queue.should_pop():
                item = self.queue.pop()
                item.callback()
            time.sleep(0.5)


if __name__ == '__main__':
    q = CallbackQueue()
    w = CallbackQueueWorkerThread(q)
    import random
    for j in range(10):
        r = random.randint(0, 2+j)
        q.add(CallbackItem(r, int, r))
    w.start()
    w.join()
