// Package llmproxy is the control plane's internal, OpenAI-compatible proxy in front
// of the LiteLLM gateway (handoff §3.8).
//
// The brain never holds the gateway key: each brain subprocess gets a per-job token
// for this proxy. The proxy maps the token to the job's priority and trace directory,
// waits on a per-model rate limiter (the gateway limits each model separately, about
// 25 requests per minute), forwards the request with the gateway key, and writes the
// raw request and response to the job's trace (the replay cassette).
package llmproxy

import (
	"container/heap"
	"context"
	"sync"
	"time"
)

// Priorities, highest first: a reply is answered ahead of a queued review.
const (
	PriorityReply      = 0
	PriorityRecheck    = 1
	PriorityReview     = 2
	PriorityBackground = 3
)

// PriorityFromName maps the brain's priority names; unknown names get review.
func PriorityFromName(name string) int {
	switch name {
	case "reply":
		return PriorityReply
	case "recheck":
		return PriorityRecheck
	case "background":
		return PriorityBackground
	default:
		return PriorityReview
	}
}

type waiter struct {
	prio  int
	seq   uint64
	ready chan struct{}
	index int
}

type waitQueue []*waiter

func (q waitQueue) Len() int { return len(q) }
func (q waitQueue) Less(i, j int) bool {
	if q[i].prio != q[j].prio {
		return q[i].prio < q[j].prio
	}
	return q[i].seq < q[j].seq
}
func (q waitQueue) Swap(i, j int) { q[i], q[j] = q[j], q[i]; q[i].index = i; q[j].index = j }
func (q *waitQueue) Push(x any)   { w := x.(*waiter); w.index = len(*q); *q = append(*q, w) }
func (q *waitQueue) Pop() any {
	old := *q
	w := old[len(old)-1]
	*q = old[:len(old)-1]
	w.index = -1
	return w
}

// Limiter is a token bucket with priorities and AIMD for one model.
type Limiter struct {
	mu           sync.Mutex
	maxRPM       float64
	rpm          float64
	minRPM       float64
	burst        float64
	tokens       float64
	stamp        time.Time
	pausedUntil  time.Time
	successes    int
	recoverAfter int
	backoff      time.Duration
	queue        waitQueue
	seq          uint64
	wake         chan struct{}
	throttles    int
	granted      int
	now          func() time.Time
}

// NewLimiter returns a limiter allowing rpm requests per minute with the given burst.
func NewLimiter(rpm, burst float64) *Limiter {
	if burst < 1 {
		burst = 1
	}
	l := &Limiter{maxRPM: rpm, rpm: rpm, minRPM: minF(2, rpm), burst: burst, tokens: burst,
		recoverAfter: 10, backoff: 5 * time.Second, wake: make(chan struct{}, 1), now: time.Now}
	l.stamp = l.now()
	return l
}

func minF(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func (l *Limiter) refill(now time.Time) {
	elapsed := now.Sub(l.stamp).Seconds()
	if elapsed > 0 {
		l.tokens += elapsed * l.rpm / 60
		if l.tokens > l.burst {
			l.tokens = l.burst
		}
	}
	l.stamp = now
}

// waitFor returns how long the head of the queue must wait (0: may go now).
func (l *Limiter) waitFor(now time.Time) time.Duration {
	if now.Before(l.pausedUntil) {
		return l.pausedUntil.Sub(now)
	}
	if l.tokens >= 1 {
		return 0
	}
	return time.Duration((1 - l.tokens) * 60 / l.rpm * float64(time.Second))
}

func (l *Limiter) signal() {
	select {
	case l.wake <- struct{}{}:
	default:
	}
}

// Acquire blocks until a request at priority prio may be sent, or ctx ends.
func (l *Limiter) Acquire(ctx context.Context, prio int) (time.Duration, error) {
	start := l.now()
	l.mu.Lock()
	l.seq++
	w := &waiter{prio: prio, seq: l.seq, ready: make(chan struct{})}
	heap.Push(&l.queue, w)
	l.mu.Unlock()
	l.signal()
	for {
		l.mu.Lock()
		now := l.now()
		l.refill(now)
		wait := l.waitFor(now)
		if l.queue[0] == w && wait == 0 {
			heap.Pop(&l.queue)
			l.tokens--
			l.granted++
			l.mu.Unlock()
			l.signal()
			return l.now().Sub(start), nil
		}
		if l.queue[0] != w || wait > 250*time.Millisecond {
			wait = minDur(maxDur(wait, 5*time.Millisecond), 250*time.Millisecond)
		}
		l.mu.Unlock()
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			l.mu.Lock()
			if w.index >= 0 {
				heap.Remove(&l.queue, w.index)
			}
			l.mu.Unlock()
			l.signal()
			return l.now().Sub(start), ctx.Err()
		case <-timer.C:
		case <-l.wake:
			timer.Stop()
		}
	}
}

func minDur(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

func maxDur(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

// OnSuccess records a successful request; after a run of them the rate creeps back up.
func (l *Limiter) OnSuccess() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.successes++
	if l.successes >= l.recoverAfter && l.rpm < l.maxRPM {
		l.rpm++
		if l.rpm > l.maxRPM {
			l.rpm = l.maxRPM
		}
		l.successes = 0
	}
}

// OnThrottle records a 429: the rate halves and everything pauses for retryAfter
// (or the default backoff).
func (l *Limiter) OnThrottle(retryAfter time.Duration) time.Duration {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.throttles++
	l.successes = 0
	l.rpm /= 2
	if l.rpm < l.minRPM {
		l.rpm = l.minRPM
	}
	if retryAfter <= 0 {
		retryAfter = l.backoff
	}
	now := l.now()
	if until := now.Add(retryAfter); until.After(l.pausedUntil) {
		l.pausedUntil = until
	}
	l.tokens = 0
	l.stamp = now
	return retryAfter
}

// Stats is a snapshot for logs and tests.
type Stats struct {
	RPM       float64
	Throttles int
	Granted   int
	Waiting   int
}

func (l *Limiter) Stats() Stats {
	l.mu.Lock()
	defer l.mu.Unlock()
	return Stats{RPM: l.rpm, Throttles: l.throttles, Granted: l.granted, Waiting: len(l.queue)}
}

// Pool holds one limiter per model, created on first use.
type Pool struct {
	mu       sync.Mutex
	rpm      float64
	burst    float64
	limiters map[string]*Limiter
}

func NewPool(rpm, burst float64) *Pool {
	return &Pool{rpm: rpm, burst: burst, limiters: map[string]*Limiter{}}
}

func (p *Pool) For(model string) *Limiter {
	p.mu.Lock()
	defer p.mu.Unlock()
	l, ok := p.limiters[model]
	if !ok {
		l = NewLimiter(p.rpm, p.burst)
		p.limiters[model] = l
	}
	return l
}
