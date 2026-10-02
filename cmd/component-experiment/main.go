package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Gontafi/university-stuff/internal/resilience"
)

type transport func(*http.Request) (*http.Response, error)

func (f transport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func main() {
	dir := "results/component"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		panic(err)
	}
	for _, ft := range []bool{false, true} {
		mode := "baseline"
		if ft {
			mode = "ft"
		}
		c := resilience.New(ft)
		c.Backoff = time.Millisecond
		remaining := 0
		c.HTTP = &http.Client{Transport: transport(func(r *http.Request) (*http.Response, error) {
			status := 200
			if remaining > 0 {
				remaining--
				status = 503
			}
			return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
		})}
		started := time.Now()
		samples := []map[string]any{}
		injected := 0.0
		cleared := 0.0
		for i := 0; i < 100; i++ {
			at := time.Since(started).Seconds()
			if i%5 == 4 {
				remaining = 1
				if injected == 0 {
					injected = at
				}
				cleared = at
			} else {
				remaining = 0
			}
			begin := time.Now()
			status, _, attempts, err := c.Do(context.Background(), "GET", "http://in-process/students", nil, http.Header{})
			latency := time.Since(begin).Seconds()
			ok := err == nil && status == 200
			samples = append(samples, map[string]any{"t": at, "end_t": time.Since(started).Seconds(), "kind": "probe", "status": status, "latency_s": latency, "http_ok": ok, "sla_ok": ok && latency <= 1.5, "recovered": ok && attempts > 1, "degraded": false})
			time.Sleep(time.Millisecond)
		}
		raw := map[string]any{"environment": "in-process simulated dependency; no PostgreSQL or Kubernetes", "mode": mode, "scenario": "transient_dependency", "utc": time.Now().UTC(), "duration_s": time.Since(started).Seconds(), "injected_at_s": injected, "cleared_at_s": cleared, "samples": samples, "consistent": nil, "observations": map[string]any{"injected_failures": 20, "operations": 100, "data_consistency": "not evaluated", "method": "One synthetic HTTP 503 on every fifth logical operation; production resilience.Client with an in-process RoundTripper"}}
		file, err := os.Create(filepath.Join(dir, mode+".json"))
		if err != nil {
			panic(err)
		}
		encoder := json.NewEncoder(file)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(raw); err != nil {
			panic(err)
		}
		file.Close()
	}
}
