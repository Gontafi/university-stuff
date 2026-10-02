package resilience

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
	"time"
)

var ErrOpen = errors.New("dependency circuit is open")

type Breaker struct {
	mu         sync.Mutex
	failures   int
	until      time.Time
	probing    bool
	generation uint64
	Threshold  int
	Cooldown   time.Duration
}

func (b *Breaker) allow() (bool, uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.until.IsZero() {
		return true, b.generation
	}
	if time.Now().Before(b.until) || b.probing {
		return false, b.generation
	}
	b.probing = true
	return true, b.generation
}

func (b *Breaker) done(ok bool, generation uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if generation != b.generation {
		return
	}
	probe := b.probing
	b.probing = false
	if ok {
		if probe {
			b.generation++
		}
		b.failures = 0
		b.until = time.Time{}
		return
	}
	b.failures++
	if b.failures >= b.Threshold || probe {
		b.until = time.Now().Add(b.Cooldown)
		b.generation++
	}
}

type Client struct {
	HTTP     *http.Client
	Enabled  bool
	Timeout  time.Duration
	Backoff  time.Duration
	Cooldown time.Duration
	Attempts int
	mu       sync.Mutex
	breakers map[string]*Breaker
	Retries  atomic.Int64
	Rejected atomic.Int64
}

func New(enabled bool) *Client {
	return &Client{HTTP: &http.Client{Timeout: 5 * time.Second}, Enabled: enabled, Timeout: 700 * time.Millisecond, Backoff: 50 * time.Millisecond, Cooldown: 3 * time.Second, Attempts: 3, breakers: map[string]*Breaker{}}
}

func (c *Client) breaker(host string) *Breaker {
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.breakers[host]
	if b == nil {
		b = &Breaker{Threshold: 3, Cooldown: c.Cooldown}
		c.breakers[host] = b
	}
	return b
}

func (c *Client) Do(ctx context.Context, method, url string, body []byte, headers http.Header) (int, []byte, int, error) {
	attempts := 1
	if c.Enabled && (method == http.MethodGet || headers.Get("Idempotency-Key") != "") {
		attempts = c.Attempts
	}
	var lastErr error
	for i := 0; i < attempts; i++ {
		req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(body))
		if err != nil {
			return 0, nil, i, err
		}
		req.Header = headers.Clone()
		b := c.breaker(req.URL.Host)
		var generation uint64
		if c.Enabled {
			allowed, current := b.allow()
			generation = current
			if !allowed {
				c.Rejected.Add(1)
				return 0, nil, i, ErrOpen
			}
		}
		cancel := func() {}
		if c.Enabled {
			var attemptCtx context.Context
			attemptCtx, cancel = context.WithTimeout(ctx, c.Timeout)
			req = req.WithContext(attemptCtx)
		}
		resp, err := c.HTTP.Do(req)
		status := 0
		var payload []byte
		if err == nil {
			status = resp.StatusCode
			payload, err = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
			resp.Body.Close()
		}
		cancel()
		ok := err == nil && status < 500 && status != 429
		if c.Enabled {
			b.done(ok, generation)
		}
		if ok {
			return status, payload, i + 1, nil
		}
		if err == nil {
			err = errors.New(http.StatusText(status))
		}
		lastErr = err
		if i+1 < attempts {
			c.Retries.Add(1)
			wait := c.Backoff * time.Duration(1<<i)
			if wait > 0 {
				wait += time.Duration(rand.Int64N(int64(wait/2) + 1))
			}
			t := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				t.Stop()
				return 0, nil, i + 1, ctx.Err()
			case <-t.C:
			}
		}
	}
	return 0, nil, attempts, lastErr
}
