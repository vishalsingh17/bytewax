"""Low-level input interfaces and input helpers.

If you want pre-built connectors for various external systems, see
`bytewax.connectors`. That is also a rich source of examples.

Subclass the types here to implement input for your own custom source.

"""

import asyncio
import queue
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Callable, Iterable, Iterator, List, Optional

__all__ = [
    "DynamicSource",
    "FixedPartitionedSource",
    "SimplePollingSource",
    "Source",
    "StatefulSourcePartition",
    "StatelessSourcePartition",
    "batch",
    "batch_async",
    "batch_getter",
    "batch_getter_ex",
]


class Source(ABC):  # noqa: B024
    """A location to read input items from.

    Base class for all input sources. Do not subclass this.

    If you want to implement a custom connector, instead subclass one
    of the specific source sub-types below in this module.

    """

    def __json__(self):
        """JSON representation of this.

        This is used by the Bytewax platform internally and should
        not be overridden.

        """
        return {
            "type": type(self).__name__,
        }


class StatefulSourcePartition(ABC):
    """Input partition that maintains state of its position."""

    @abstractmethod
    def next_batch(self) -> List[Any]:
        """Attempt to get the next batch of input items.

        This must participate in a kind of cooperative multi-tasking,
        never blocking but returning an empty list if there are no
        items to emit yet.

        Returns:
            An list of items immediately ready. May be empty if no new
            items.

        Raises:
            StopIteration: When the source is complete.

        """
        ...

    def next_awake(self) -> Optional[datetime]:
        """When to next attempt to get input items.

        `next_batch()` will not be called until the most recently returned
        time has past.

        This will be called upon initialization of the source and
        after `next_batch()`, but also possibly at other times. Multiple
        times are not stored; you must return the next awake time on
        every call, if any.

        If this returns `None`, `next_batch()` will be called
        immediately unless the previous batch had no items, in which
        case there is a 1 millisecond delay.

        Use this instead of `time.sleep` in `next_batch()`.

        Returns:
            Next awake time or `None` to indicate automatic behavior.

        """
        return None

    @abstractmethod
    def snapshot(self) -> Any:
        """Snapshot the position of the next read of this partition.

        This will be returned to you via the `resume_state` parameter
        of your input builder.

        Be careful of "off by one" errors in resume state. This should
        return a state that, when built into a partition, resumes reading
        _after the last read item item_, not the same item that
        `next()` last returned.

        This is guaranteed to never be called after `close()`.

        Returns:
            Resume state.

        """
        ...

    def close(self) -> None:
        """Cleanup this partition when the dataflow completes.

        This is not guaranteed to be called. It will only be called
        when the dataflow finishes on finite input. It will not be
        called during an abrupt or abort shutdown.

        """
        return


class FixedPartitionedSource(Source):
    """An input source with a fixed number of independent partitions.

    Will maintain the state of each source and re-build using it
    during resume. If the source supports seeking, this input can
    support exactly-once processing.

    Each partition must contain unique data. If you re-read the same data
    in multiple partitions, the dataflow will process these duplicate
    items.

    """

    @abstractmethod
    def list_parts(self) -> List[str]:
        """List all local partitions this worker has access to.

        You do not need to list all partitions globally.

        Returns:
            Local partition keys.

        """
        ...

    @abstractmethod
    def build_part(
        self,
        for_part: str,
        resume_state: Optional[Any],
    ) -> StatefulSourcePartition:
        """Build anew or resume an input partition.

        Will be called once per execution for each partition key on a
        worker that reported that partition was local in `list_parts`.

        Do not pre-build state about a partition in the
        constructor. All state must be derived from `resume_state` for
        recovery to work properly.

        Args:
            for_part:
                Which partition to build. Will always be one of the
                keys returned by `list_parts` on this worker.

            resume_state:
                State data containing where in the input stream this
                partition should be begin reading during this
                execution.

        Returns:
            The built partition.

        """
        ...


class StatelessSourcePartition(ABC):
    """Input partition that is stateless."""

    @abstractmethod
    def next_batch(self) -> List[Any]:
        """Attempt to get the next batch of input items.

        This must participate in a kind of cooperative multi-tasking,
        never blocking but yielding an empty list if there are no new
        items yet.

        Returns:
            An list of items immediately ready. May be empty if no new
            items.

        Raises:
            StopIteration: When the source is complete.

        """
        ...

    def next_awake(self) -> Optional[datetime]:
        """When to next attempt to get input items.

        `next_batch()` will not be called until the most recently returned
        time has past.

        This will be called upon initialization of the source and
        after `next_batch()`, but also possibly at other times. Multiple
        times are not stored; you must return the next awake time on
        every call, if any.

        If this returns `None`, `next_batch()` will be called
        immediately unless the previous batch had no items, in which
        case there is a 1 millisecond delay.

        Use this instead of `time.sleep` in `next_batch()`.

        Returns:
            Next awake time or `None` to indicate automatic behavior.

        """
        return None

    def close(self) -> None:
        """Cleanup this partition when the dataflow completes.

        This is not guaranteed to be called. It will only be called
        when the dataflow finishes on finite input. It will not be
        called during an abrupt or abort shutdown.

        """
        return


class DynamicSource(Source):
    """An input source where all workers can read distinct items.

    Does not support storing any resume state. Thus these kind of
    sources only naively can support at-most-once processing.

    The source must somehow support supplying disjoint data for each
    worker. If you re-read the same items on multiple workers, the
    dataflow will process these as duplicate items.

    """

    @abstractmethod
    def build(self, worker_index, worker_count) -> StatelessSourcePartition:
        """Build an input source for a worker.

        Will be called once on each worker.

        Args:
            worker_index:
                Index of this worker.
            worker_count:
                Total number of workers.

        Returns:
            The built partition.

        """
        ...


class _SimplePollingPartition(StatefulSourcePartition):
    def __init__(
        self, interval, align_to, getter, now_getter=lambda: datetime.now(timezone.utc)
    ):
        self._interval = interval
        self._getter = getter

        now = now_getter()
        if align_to is not None:
            # Hell yeah timedelta implements remainder.
            since_last_awake = (now - align_to) % interval
            until_next_awake = interval - since_last_awake
            self._next_awake = now + until_next_awake
        else:
            self._next_awake = now

    def next_batch(self):
        self._next_awake += self._interval
        return [self._getter()]

    def next_awake(self):
        return self._next_awake

    def snapshot(self):
        return None


class SimplePollingSource(FixedPartitionedSource):
    """Calls a user defined function at a regular interval.

    Subclass this input source and write the `next_item` function
    that will be called at the defined interval.

    Example:
    >>> class URLSource(SimplePollingSource):
    ...     def next_item(self):
    ...         return requests.get("https://example.com")
    ...

    Notes:
    - The `interval` parameter is capped at a minimum of 10ms, but it can be as
      large as needed.
    - If you need fast input polling, consider writing a custom input source,
      where you can also batch items to avoid doing too much IO.
    """

    def __init__(self, interval: timedelta, align_to: Optional[datetime] = None):
        """Create a PeriodicInput.

        Args:
            interval: The interval between awakes. Must be >= 10ms.
            align_to: Align awake times to the given datetime.
        """
        if interval < timedelta(milliseconds=10):
            msg = "The interval for SimplePollingInput must be >= 10ms"
            raise ValueError(msg)
        self._interval = interval
        self._align_to = align_to

    def list_parts(self):
        """Only emit a single tick."""
        return ["singleton"]

    def build_part(self, _for_part, _resume_state):
        """See ABC docstring."""
        return _SimplePollingPartition(self._interval, self._align_to, self.next_item)

    @abstractmethod
    def next_item(self) -> Any:
        """This function will be called at regular inerval."""
        ...


def batch(ib: Iterable[Any], batch_size: int) -> Iterator[List[Any]]:
    """Batch an iterable.

    Use this to easily generate batches of items for a source's
    `next_batch` method.

    Args:
        ib:
            The underlying source iterable of items.
        batch_size:
            Maximum number of items to yield in a batch.

    Yields:
        The next gathered batch of items.

    """
    # Ensure that we have the stateful iterator of the source.
    it = iter(ib)
    while True:
        batch = list(islice(it, batch_size))
        if len(batch) <= 0:
            return
        yield batch


def batch_getter(
    getter: Callable[[], Any], batch_size: int, yield_on: Any = None
) -> Iterator[List[Any]]:
    """Batch from a getter function that might not return an item.

     Use this to easily generate batches of items for a source's
    `next_batch` method.

    Args:
        getter:
            Function to call to get the next item. Should raise
            `StopIteration` on EOF.

        batch_size:
            Maximum number of items to yield in a batch.

        yield_on:
            Sentinel value that indicates that there are no more items
            yet, and to return the current batch. Defaults to `None`.

    Yields:
        The next gathered batch of items.

    """
    while True:
        batch = []
        while len(batch) < batch_size:
            try:
                item = getter()
                if item != yield_on:
                    batch.append(item)
                else:
                    break
            except StopIteration:
                yield batch
                return
        yield batch


def batch_getter_ex(
    getter: Callable[[], Any], batch_size: int, yield_ex: Exception = queue.Empty
) -> Iterator[List[Any]]:
    """Batch from a getter function that raises on no items yet.

     Use this to easily generate batches of items for a source's
    `next_batch` method.

    Args:
        getter:
            Function to call to get the next item. Should raise
            `StopIteration` on EOF.

        batch_size:
            Maximum number of items to return in a batch.

        yield_ex:
            Exception raised by `getter` that indicates that there are
            no more items yet, and to return the current
            batch. Defaults to `queue.Empty`.

    Yields:
        The next gathered batch of items.

    """
    while True:
        batch = []
        while len(batch) < batch_size:
            try:
                item = getter()
                batch.append(item)
            except yield_ex:
                break
            except StopIteration:
                yield batch
                return
        yield batch


def batch_async(
    aib: AsyncIterable,
    timeout: timedelta,
    batch_size: int,
    loop=None,
) -> Iterator[List[Any]]:
    """Batch an async iterable synchronously up to a timeout.

    This allows using an async iterator as an input source. The
    `next_batch` method on an input source must never block, this
    allows running an async iterator up to a timeout so that you
    correctly cooperatively multitask with the rest of the dataflow.

    Args:
        aib:
            The underlying source async iterable of items.
        timeout:
            Duration of time to repeatedly poll the source
            async iterator for items.
        batch_size:
            Maximum number of items to yield in a batch, even if
            the timeout has not been hit.
        loop:
            Custom `asyncio` run loop to use, if any.

    Yields:
        The next gathered batch of items.

        This function will take up to `timeout` time to yield, or
        will return a list with length up to `max_len`.

    """
    # Ensure that we have the stateful iterator of the source.
    ait = aib.__aiter__()

    loop = loop if loop is not None else asyncio.new_event_loop()
    task = None

    async def anext_batch():
        nonlocal task

        batch = []
        # Only try to gather this many items.
        for _ in range(batch_size):
            if task is None:
                task = loop.create_task(ait.__anext__())

            try:
                # Prevent the `wait_for` cancellation from
                # stopping the `__anext__` task; usually all
                # sub-tasks are cancelled too. It'll be re-used in
                # the next batch.
                next_item = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Timeout was hit and thus return the batch
                # immediately.
                break
            except StopAsyncIteration:
                if len(batch) > 0:
                    # Return a half-finished batch if we run out
                    # of source items.
                    break
                else:
                    # We can't raise `StopIteration` directly here
                    # because it's part of the coro protocol and
                    # would mess with this async function.
                    raise

            batch.append(next_item)
            task = None
        return batch

    while True:
        try:
            # `wait_for` will raise `CancelledError` at the internal
            # await point in `anext_batch` if the timeout is hit.
            batch = loop.run_until_complete(
                asyncio.wait_for(anext_batch(), timeout.total_seconds())
            )
            yield batch
        except StopAsyncIteration:
            return
