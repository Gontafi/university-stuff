package resilience

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func response(status int) *http.Response {
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(`{"ok":true}`)), Header: http.Header{}}
}
func clientWith(ft bool, f transportFunc) *Client {
	c := New(ft)
	c.Backoff = 0
	c.Cooldown = 10 * time.Millisecond
	c.Timeout = 10 * time.Millisecond
	c.HTTP = &http.Client{Transport: f}
	return c
}

func TestTransientFailure(t *testing.T) {
	for _, ft := range []bool{false, true} {
		t.Run(map[bool]string{false: "baseline", true: "ft"}[ft], func(t *testing.T) {
			calls := 0
			c := clientWith(ft, func(r *http.Request) (*http.Response, error) {
				calls++
				if calls == 1 {
					return response(503), nil
				}
				return response(200), nil
			})
			status, _, attempts, err := c.Do(context.Background(), "GET", "http://service/students", nil, http.Header{})
			if ft && (err != nil || status != 200 || attempts != 2) {
				t.Fatalf("retry result: status=%d attempts=%d error=%v", status, attempts, err)
			}
			if !ft && (err == nil || calls != 1) {
				t.Fatal("baseline unexpectedly retried")
			}
		})
	}
}

func TestUnsafeWriteIsNotRetried(t *testing.T) {
	calls := 0
	c := clientWith(true, func(r *http.Request) (*http.Response, error) { calls++; return response(503), nil })
	c.Do(context.Background(), "POST", "http://service/students", []byte(`{}`), http.Header{})
	if calls != 1 {
		t.Fatalf("unsafe write called %d times", calls)
	}
}

func TestKeyedWritePreservesPayload(t *testing.T) {
	calls := 0
	c := clientWith(true, func(r *http.Request) (*http.Response, error) {
		calls++
		body, _ := io.ReadAll(r.Body)
		if string(body) != `{"amount":100}` || r.Header.Get("Idempotency-Key") != "same-key" {
			t.Fatal("retry changed request")
		}
		if calls == 1 {
			return nil, io.ErrUnexpectedEOF
		}
		return response(201), nil
	})
	status, _, attempts, err := c.Do(context.Background(), "POST", "http://service/payments", []byte(`{"amount":100}`), http.Header{"Idempotency-Key": []string{"same-key"}})
	if status != 201 || attempts != 2 || err != nil {
		t.Fatalf("%d %d %v", status, attempts, err)
	}
}

func TestTimeoutAndBreakerRecovery(t *testing.T) {
	var calls atomic.Int64
	var healthy atomic.Bool
	c := clientWith(true, func(r *http.Request) (*http.Response, error) {
		calls.Add(1)
		if healthy.Load() {
			return response(200), nil
		}
		<-r.Context().Done()
		return nil, r.Context().Err()
	})
	start := time.Now()
	c.Do(context.Background(), "GET", "http://service/students", nil, http.Header{})
	if time.Since(start) > time.Second || calls.Load() != 3 {
		t.Fatal("timeout did not bound retries")
	}
	_, _, _, err := c.Do(context.Background(), "GET", "http://service/students", nil, http.Header{})
	if !errors.Is(err, ErrOpen) || calls.Load() != 3 {
		t.Fatal("open breaker reached dependency")
	}
	healthy.Store(true)
	time.Sleep(15 * time.Millisecond)
	status, _, _, err := c.Do(context.Background(), "GET", "http://service/students", nil, http.Header{})
	if status != 200 || err != nil {
		t.Fatal("half-open probe did not recover")
	}
}

func TestConcurrentHalfOpenOnlyOneProbe(t *testing.T) {
	b := &Breaker{Threshold: 1, Cooldown: time.Millisecond}
	b.done(false, 0)
	time.Sleep(2 * time.Millisecond)
	var probes atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if allowed, _ := b.allow(); allowed {
				probes.Add(1)
			}
		}()
	}
	wg.Wait()
	if probes.Load() != 1 {
		t.Fatalf("half-open probes=%d", probes.Load())
	}
	b.done(true, b.generation)
	if allowed, _ := b.allow(); !allowed {
		t.Fatal("closed breaker denied request")
	}
}

func TestStaleSuccessDoesNotCloseOpenCircuit(t *testing.T) {
	b := &Breaker{Threshold: 1, Cooldown: time.Second}
	_, failedGeneration := b.allow()
	_, lateGeneration := b.allow()
	b.done(false, failedGeneration)
	b.done(true, lateGeneration)
	if allowed, _ := b.allow(); allowed {
		t.Fatal("stale response closed an open circuit")
	}
}

func TestClientErrorsAreNotRetried(t *testing.T) {
	calls := 0
	c := clientWith(true, func(r *http.Request) (*http.Response, error) { calls++; return response(409), nil })
	status, _, _, err := c.Do(context.Background(), "POST", "http://service/payments", nil, http.Header{"Idempotency-Key": []string{"conflict"}})
	if status != 409 || err != nil || calls != 1 {
		t.Fatal("client conflict was retried")
	}
}

func TestCancelledParent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	c := clientWith(true, func(r *http.Request) (*http.Response, error) { return nil, r.Context().Err() })
	c.Backoff = time.Second
	start := time.Now()
	_, _, _, err := c.Do(ctx, "GET", "http://service/students", nil, http.Header{})
	if !errors.Is(err, context.Canceled) || time.Since(start) > 100*time.Millisecond {
		t.Fatal("cancelled request waited for retry")
	}
}
