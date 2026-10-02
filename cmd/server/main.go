package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Gontafi/university-stuff/internal/app"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		slog.Error("database configuration", "error", err)
		os.Exit(1)
	}
	defer pool.Close()
	ready := false
	for i := 0; i < 60; i++ {
		attempt, cancel := context.WithTimeout(ctx, 2*time.Second)
		err = app.Migrate(attempt, pool)
		cancel()
		if err == nil {
			ready = true
			break
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
	if !ready {
		slog.Error("database startup", "error", err)
		os.Exit(1)
	}
	a := app.New(pool, os.Getenv("ROLE"), os.Getenv("MODE") == "ft")
	server := &http.Server{Addr: ":8080", Handler: a.Handler(), ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	slog.Info("service started", "role", a.Role, "fault_tolerant", a.FT)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("server", "error", err)
		os.Exit(1)
	}
}
