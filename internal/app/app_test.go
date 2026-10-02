package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestCachedTranscriptDegradation(t *testing.T) {
	a := New(nil, "gateway", true)
	a.Client.Backoff = 0
	up := true
	a.Client.HTTP = &http.Client{Transport: roundTrip(func(r *http.Request) (*http.Response, error) {
		status := 200
		data := `{"student":{"id":1}}`
		if !up {
			status = 503
			data = `{}`
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(data)), Header: http.Header{}}, nil
	})}
	h := a.Handler()
	request := func(path string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		return w
	}
	if request("/api/transcript?student_id=1").Code != 200 {
		t.Fatal("cache warmup failed")
	}
	up = false
	w := request("/api/transcript?student_id=1")
	if w.Code != 200 || w.Header().Get("X-Degraded") == "" || !strings.Contains(w.Body.String(), `"id":1`) {
		t.Fatal("cached transcript not marked or returned")
	}
	if request("/api/transcript?student_id=2").Code != 503 {
		t.Fatal("cache leaked another student's transcript")
	}
	a.mu.Lock()
	a.cache["/transcript?student_id=1"] = cached{data: []byte(`{}`), at: time.Now().Add(-61 * time.Second)}
	a.mu.Unlock()
	if request("/api/transcript?student_id=1").Code != 503 {
		t.Fatal("expired transcript served")
	}
}

func TestFaultInjectionDisabled(t *testing.T) {
	t.Setenv("FAULT_TOKEN", "")
	a := New(nil, "gateway", true)
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, httptest.NewRequest("POST", "/api/faults", strings.NewReader(`{}`)))
	if w.Code != 403 {
		t.Fatal("fault injection enabled without token")
	}
}

func TestPaymentTransactionsPostgres(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL is required; use a disposable PostgreSQL database")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if err = Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAULT_TOKEN", "test-only")
	var student int64
	if err = pool.QueryRow(ctx, "INSERT INTO students(name) VALUES('transaction-test') RETURNING id").Scan(&student); err != nil {
		t.Fatal(err)
	}
	defer func() {
		pool.Exec(ctx, "DELETE FROM payments WHERE student_id=$1", student)
		pool.Exec(ctx, "DELETE FROM students WHERE id=$1", student)
	}()
	a := New(pool, "payment", true)
	h := a.Handler()
	key := fmt.Sprintf("test-%d", student)
	call := func(amount int, interrupt bool) *httptest.ResponseRecorder {
		body, _ := json.Marshal(map[string]any{"student_id": student, "amount": amount})
		r := httptest.NewRequest("POST", "/payments", bytes.NewReader(body))
		r.Header.Set("Idempotency-Key", key)
		if interrupt {
			r.Header.Set("X-Fault-Token", "test-only")
			r.Header.Set("X-Interrupt", "true")
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if call(100, true).Code != 503 {
		t.Fatal("interrupt not injected")
	}
	var count, paid int64
	pool.QueryRow(ctx, "SELECT count(*) FROM payments WHERE student_id=$1", student).Scan(&count)
	if count != 0 {
		t.Fatal("interrupted ledger insert was not rolled back")
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := call(100, false)
			if w.Code != 200 && w.Code != 201 {
				t.Errorf("concurrent payment: %d %s", w.Code, w.Body.String())
			}
		}()
	}
	wg.Wait()
	pool.QueryRow(ctx, "SELECT count(*) FROM payments WHERE student_id=$1", student).Scan(&count)
	pool.QueryRow(ctx, "SELECT paid FROM students WHERE id=$1", student).Scan(&paid)
	if count != 1 || paid != 100 {
		t.Fatalf("duplicate charge: rows=%d paid=%d", count, paid)
	}
	if call(101, false).Code != 409 {
		t.Fatal("key collision accepted another payload")
	}
}
