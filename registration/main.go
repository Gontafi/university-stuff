// Registration service for the synthetic AITU student services platform (Assignment 3).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const schema = `CREATE TABLE IF NOT EXISTS registrations (
	id BIGSERIAL PRIMARY KEY,
	student_id TEXT NOT NULL CHECK (length(student_id) BETWEEN 1 AND 20),
	course TEXT NOT NULL CHECK (length(course) BETWEEN 1 AND 40),
	created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
	UNIQUE (student_id, course)
)`

type registration struct {
	ID        int64     `json:"id"`
	StudentID string    `json:"student_id"`
	Course    string    `json:"course"`
	CreatedAt time.Time `json:"created_at"`
}

type server struct {
	db       *pgxpool.Pool
	pod      string
	draining atomic.Bool
	migrated atomic.Bool
}

func main() {
	cfg, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if err != nil {
		slog.Error("bad DATABASE_URL", "err", err)
		os.Exit(1)
	}
	cfg.MaxConns = 8
	cfg.ConnConfig.ConnectTimeout = 2 * time.Second
	db, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		slog.Error("pool", "err", err)
		os.Exit(1)
	}
	pod, _ := os.Hostname()
	s := &server{db: db, pod: pod}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("GET /health/ready", s.ready)
	mux.HandleFunc("POST /registrations", s.register)
	mux.HandleFunc("GET /registrations", s.list)
	srv := &http.Server{Addr: ":8080", Handler: s.wrap(mux), ReadHeaderTimeout: 2 * time.Second, WriteTimeout: 5 * time.Second}

	go func() {
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
		<-stop
		// fail readiness first so the Service stops sending traffic, then finish open requests
		s.draining.Store(true)
		slog.Info("draining", "pod", s.pod)
		time.Sleep(4 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	}()
	slog.Info("listening", "pod", pod)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server", "err", err)
		os.Exit(1)
	}
	db.Close()
}

func (s *server) wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Pod", s.pod)
		next.ServeHTTP(w, r)
	})
}

// ready is used by the readiness probe: the pod gets traffic only when PostgreSQL answers in time.
// The liveness probe does not look at the database, so a database outage does not restart the pods.
func (s *server) ready(w http.ResponseWriter, r *http.Request) {
	if s.draining.Load() {
		http.Error(w, "draining", 503)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 300*time.Millisecond)
	defer cancel()
	if err := s.db.Ping(ctx); err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	if !s.migrated.Load() {
		if _, err := s.db.Exec(ctx, schema); err != nil {
			http.Error(w, "schema not ready", 503)
			return
		}
		s.migrated.Store(true)
	}
	w.WriteHeader(200)
}

func (s *server) register(w http.ResponseWriter, r *http.Request) {
	var in registration
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024)).Decode(&in); err != nil || in.StudentID == "" || in.Course == "" {
		http.Error(w, "student_id and course required", 400)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 400*time.Millisecond)
	defer cancel()
	// a repeated request for the same student and course returns the existing row
	err := s.db.QueryRow(ctx, `INSERT INTO registrations(student_id, course) VALUES ($1, $2)
		ON CONFLICT (student_id, course) DO UPDATE SET course = EXCLUDED.course
		RETURNING id, student_id, course, created_at`, in.StudentID, in.Course).
		Scan(&in.ID, &in.StudentID, &in.Course, &in.CreatedAt)
	if err != nil {
		slog.Warn("insert", "err", err)
		http.Error(w, "database unavailable", 503)
		return
	}
	writeJSON(w, 200, in)
}

func (s *server) list(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 400*time.Millisecond)
	defer cancel()
	rows, err := s.db.Query(ctx, `SELECT id, student_id, course, created_at FROM registrations
		WHERE student_id = $1 ORDER BY id`, r.URL.Query().Get("student_id"))
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	out, err := pgx.CollectRows(rows, pgx.RowToStructByPos[registration])
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	writeJSON(w, 200, out)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
