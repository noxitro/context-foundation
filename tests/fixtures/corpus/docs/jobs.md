# Job system notes

Synthetic test fixture for context-foundation. It describes a made-up
sample engine and is not documentation of a real product.

## Queues

Each worker thread owns a queue. A worker pushes and pops its own work at
one end of its queue, which keeps related work on the same core and its
data in that core's cache.

## Taking work from other workers

A worker that runs out of work looks at the queues of the other workers
and takes a job from the far end of one of them. It starts from a random
worker, so that several idle workers do not all go for the same queue.
Taking from the far end means the owner and the visitor rarely touch the
same job.

## Sleeping

When no queue has work, a worker sleeps for a short time and is woken as
soon as new work is submitted.
