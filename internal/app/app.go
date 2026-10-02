package app

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Gontafi/university-stuff/internal/resilience"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schema string

func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(19742)"); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, schema); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type cached struct {
	data []byte
	at   time.Time
}
type App struct {
	DB       *pgxpool.Pool
	Role     string
	FT       bool
	Client   *resilience.Client
	requests atomic.Int64
	failures atomic.Int64
	active   chan struct{}
	mu       sync.Mutex
	cache    map[string]cached
}

func New(db *pgxpool.Pool, role string, ft bool) *App {
	if role == "" {
		role = "gateway"
	}
	return &App{DB: db, Role: role, FT: ft, Client: resilience.New(ft), active: make(chan struct{}, 64), cache: map[string]cached{}}
}

func respond(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, status int, msg string) {
	respond(w, status, map[string]string{"error": msg})
}
func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 8192)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if d.Decode(v) != nil {
		fail(w, 400, "invalid JSON")
		return false
	}
	if d.Decode(new(any)) != io.EOF {
		fail(w, 400, "expected one JSON object")
		return false
	}
	return true
}

func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, r *http.Request) {
		respond(w, 200, map[string]string{"role": a.Role, "mode": os.Getenv("MODE")})
	})
	mux.HandleFunc("GET /health/ready", func(w http.ResponseWriter, r *http.Request) {
		if a.Role == "gateway" && a.FT {
			respond(w, 200, map[string]bool{"ready": true})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 500*time.Millisecond)
		defer cancel()
		if a.DB.Ping(ctx) != nil {
			fail(w, 503, "database unavailable")
			return
		}
		respond(w, 200, map[string]bool{"ready": true})
	})
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "university_requests_total %d\nuniversity_failures_total %d\nuniversity_retries_total %d\nuniversity_circuit_rejections_total %d\n", a.requests.Load(), a.failures.Load(), a.Client.Retries.Load(), a.Client.Rejected.Load())
	})
	if a.Role == "gateway" {
		mux.Handle("/", http.FileServer(http.Dir("web")))
		mux.HandleFunc("GET /api/info", func(w http.ResponseWriter, r *http.Request) {
			respond(w, 200, map[string]any{"mode": os.Getenv("MODE"), "services": []string{"student", "payment", "records"}})
		})
		mux.HandleFunc("/api/students", a.proxy("student", "/students"))
		mux.HandleFunc("/api/payments", a.proxy("payment", "/payments"))
		mux.HandleFunc("/api/grades", a.proxy("records", "/grades"))
		mux.HandleFunc("GET /api/transcript", a.proxy("records", "/transcript"))
		mux.HandleFunc("GET /api/audit", a.audit)
		mux.HandleFunc("POST /api/faults", a.faults)
	} else if a.Role == "student" {
		mux.HandleFunc("GET /students", a.students)
		mux.HandleFunc("POST /students", a.register)
	} else if a.Role == "payment" {
		mux.HandleFunc("GET /payments", a.payments)
		mux.HandleFunc("POST /payments", a.pay)
	} else if a.Role == "records" {
		mux.HandleFunc("POST /grades", a.grade)
		mux.HandleFunc("GET /transcript", a.transcript)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/health/") || r.URL.Path == "/metrics" {
			mux.ServeHTTP(w, r)
			return
		}
		started := time.Now()
		a.requests.Add(1)
		rec := &statusWriter{ResponseWriter: w, status: 200}
		defer func() {
			if rec.status >= 500 || rec.status == 429 {
				a.failures.Add(1)
			}
			slog.Info("request", "role", a.Role, "method", r.Method, "path", r.URL.Path, "status", rec.status, "duration_ms", time.Since(started).Milliseconds())
		}()
		if a.FT {
			select {
			case a.active <- struct{}{}:
				defer func() { <-a.active }()
			default:
				fail(rec, 429, "service busy")
				return
			}
			ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
			defer cancel()
			r = r.WithContext(ctx)
		}
		if a.Role != "gateway" && os.Getenv("FAULT_TOKEN") != "" {
			var delay, failures int
			if err := a.DB.QueryRow(r.Context(), "SELECT delay_ms,failures FROM faults WHERE role=$1", a.Role).Scan(&delay, &failures); err != nil {
				fail(rec, 503, "database unavailable")
				return
			}
			if delay > 0 {
				timer := time.NewTimer(time.Duration(delay) * time.Millisecond)
				defer timer.Stop()
				select {
				case <-r.Context().Done():
					fail(rec, 504, "request deadline")
					return
				case <-timer.C:
				}
			}
			if failures > 0 {
				err := a.DB.QueryRow(r.Context(), "UPDATE faults SET failures=failures-1 WHERE role=$1 AND failures>0 RETURNING failures", a.Role).Scan(&failures)
				if err == nil {
					fail(rec, 503, "injected service failure")
					return
				}
				if !errors.Is(err, pgx.ErrNoRows) {
					fail(rec, 503, "database unavailable")
					return
				}
			}
		}
		mux.ServeHTTP(rec, r)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (a *App) dbError(w http.ResponseWriter, err error) {
	if errors.Is(err, pgx.ErrNoRows) {
		fail(w, 404, "not found")
		return
	}
	var sqlErr interface{ SQLState() string }
	if errors.As(err, &sqlErr) && strings.HasPrefix(sqlErr.SQLState(), "23") {
		fail(w, 409, "conflict or invalid reference")
		return
	}
	slog.Error("database operation", "role", a.Role, "error", err)
	fail(w, 503, "database operation unavailable")
}

func (a *App) proxy(role, path string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" && r.Method != "POST" {
			fail(w, 405, "method not allowed")
			return
		}
		base := os.Getenv(strings.ToUpper(role) + "_URL")
		if base == "" {
			base = "http://" + role + ":8080"
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 8192))
		if err != nil {
			fail(w, 413, "request too large")
			return
		}
		headers := http.Header{"Content-Type": []string{"application/json"}}
		if role == "payment" {
			headers.Set("Idempotency-Key", r.Header.Get("Idempotency-Key"))
		}
		if os.Getenv("FAULT_TOKEN") != "" && r.Header.Get("X-Fault-Token") == os.Getenv("FAULT_TOKEN") {
			headers.Set("X-Fault-Token", r.Header.Get("X-Fault-Token"))
			headers.Set("X-Interrupt", r.Header.Get("X-Interrupt"))
		}
		key := path + "?" + r.URL.RawQuery
		status, data, attempts, err := a.Client.Do(r.Context(), r.Method, base+key, body, headers)
		if err != nil {
			if a.FT && path == "/transcript" {
				a.mu.Lock()
				entry, ok := a.cache[key]
				a.mu.Unlock()
				if ok && time.Since(entry.at) < 60*time.Second {
					w.Header().Set("X-Degraded", "cached-transcript")
					w.Header().Set("Warning", `110 - "Response is stale"`)
					w.Header().Set("Content-Type", "application/json")
					w.Write(entry.data)
					return
				}
			}
			fail(w, 503, err.Error())
			return
		}
		if a.FT && path == "/transcript" && status == 200 {
			a.mu.Lock()
			if len(a.cache) >= 1000 {
				clear(a.cache)
			}
			a.cache[key] = cached{data: data, at: time.Now()}
			a.mu.Unlock()
		}
		if attempts > 1 {
			w.Header().Set("X-Recovered", "retry")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write(data)
	}
}

func studentID(r *http.Request) (int64, error) {
	id, err := strconv.ParseInt(r.URL.Query().Get("student_id"), 10, 64)
	if err != nil || id <= 0 {
		return 0, errors.New("positive student_id required")
	}
	return id, nil
}

func (a *App) faults(w http.ResponseWriter, r *http.Request) {
	if os.Getenv("FAULT_TOKEN") == "" || r.Header.Get("X-Fault-Token") != os.Getenv("FAULT_TOKEN") {
		fail(w, 403, "fault injection disabled or invalid token")
		return
	}
	var f struct {
		Role     string `json:"role"`
		Delay    int    `json:"delay_ms"`
		Failures int    `json:"failures"`
	}
	if !decode(w, r, &f) {
		return
	}
	if (f.Role != "student" && f.Role != "payment" && f.Role != "records") || f.Delay < 0 || f.Delay > 10000 || f.Failures < 0 || f.Failures > 1000 {
		fail(w, 400, "invalid fault")
		return
	}
	_, err := a.DB.Exec(r.Context(), "UPDATE faults SET delay_ms=$2, failures=$3 WHERE role=$1", f.Role, f.Delay, f.Failures)
	if err != nil {
		a.dbError(w, err)
		return
	}
	respond(w, 200, f)
}

func (a *App) audit(w http.ResponseWriter, r *http.Request) {
	id := int64(0)
	if r.URL.Query().Get("student_id") != "" {
		var err error
		id, err = studentID(r)
		if err != nil {
			fail(w, 400, err.Error())
			return
		}
	}
	var mismatches, duplicates int
	err := a.DB.QueryRow(r.Context(), `SELECT count(*) FROM students s WHERE ($1::bigint=0 OR s.id=$1) AND s.paid <> COALESCE((SELECT sum(amount) FROM payments p WHERE p.student_id=s.id),0)`, id).Scan(&mismatches)
	if err != nil {
		a.dbError(w, err)
		return
	}
	err = a.DB.QueryRow(r.Context(), `SELECT count(*) FROM (SELECT request_key FROM payments WHERE request_key IS NOT NULL GROUP BY request_key HAVING count(*)>1) p`).Scan(&duplicates)
	if err != nil {
		a.dbError(w, err)
		return
	}
	respond(w, 200, map[string]int{"balance_mismatches": mismatches, "duplicate_keys": duplicates})
}
