package app

import (
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type Student struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
	Paid int64  `json:"paid"`
}
type Payment struct {
	ID        int64 `json:"id"`
	StudentID int64 `json:"student_id"`
	Amount    int64 `json:"amount"`
}

func (a *App) students(w http.ResponseWriter, r *http.Request) {
	rows, err := a.DB.Query(r.Context(), "SELECT id,name,paid FROM students ORDER BY id LIMIT 200")
	if err != nil {
		a.dbError(w, err)
		return
	}
	defer rows.Close()
	out := []Student{}
	for rows.Next() {
		var s Student
		if err := rows.Scan(&s.ID, &s.Name, &s.Paid); err != nil {
			a.dbError(w, err)
			return
		}
		out = append(out, s)
	}
	if rows.Err() != nil {
		a.dbError(w, rows.Err())
		return
	}
	respond(w, 200, out)
}

func (a *App) register(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Name string `json:"name"`
	}
	if !decode(w, r, &in) {
		return
	}
	in.Name = strings.TrimSpace(in.Name)
	if len(in.Name) == 0 || len(in.Name) > 100 {
		fail(w, 400, "name must contain 1–100 bytes")
		return
	}
	s := Student{Name: in.Name}
	if err := a.DB.QueryRow(r.Context(), "INSERT INTO students(name) VALUES($1) RETURNING id,paid", in.Name).Scan(&s.ID, &s.Paid); err != nil {
		a.dbError(w, err)
		return
	}
	respond(w, 201, s)
}

func (a *App) payments(w http.ResponseWriter, r *http.Request) {
	id, err := studentID(r)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	rows, err := a.DB.Query(r.Context(), "SELECT id,student_id,amount FROM payments WHERE student_id=$1 ORDER BY id", id)
	if err != nil {
		a.dbError(w, err)
		return
	}
	defer rows.Close()
	out := []Payment{}
	for rows.Next() {
		var p Payment
		if err := rows.Scan(&p.ID, &p.StudentID, &p.Amount); err != nil {
			a.dbError(w, err)
			return
		}
		out = append(out, p)
	}
	if rows.Err() != nil {
		a.dbError(w, rows.Err())
		return
	}
	respond(w, 200, out)
}

func (a *App) pay(w http.ResponseWriter, r *http.Request) {
	var p Payment
	if !decode(w, r, &p) {
		return
	}
	if p.ID != 0 || p.StudentID <= 0 || p.Amount <= 0 || p.Amount > 100000000 {
		fail(w, 400, "positive student_id and amount in minor currency units required")
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if a.FT && (len(key) < 1 || len(key) > 100) {
		fail(w, 400, "Idempotency-Key (1–100 bytes) required")
		return
	}
	interrupt := os.Getenv("FAULT_TOKEN") != "" && r.Header.Get("X-Fault-Token") == os.Getenv("FAULT_TOKEN") && r.Header.Get("X-Interrupt") == "true"
	if !a.FT {
		err := a.DB.QueryRow(r.Context(), "INSERT INTO payments(student_id,amount) VALUES($1,$2) RETURNING id", p.StudentID, p.Amount).Scan(&p.ID)
		if err != nil {
			a.dbError(w, err)
			return
		}
		if interrupt {
			fail(w, 503, "interrupted after ledger insert")
			return
		}
		if _, err = a.DB.Exec(r.Context(), "UPDATE students SET paid=paid+$2 WHERE id=$1", p.StudentID, p.Amount); err != nil {
			a.dbError(w, err)
			return
		}
		respond(w, 201, p)
		return
	}
	tx, err := a.DB.Begin(r.Context())
	if err != nil {
		a.dbError(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", key); err != nil {
		a.dbError(w, err)
		return
	}
	var previous Payment
	err = tx.QueryRow(r.Context(), "SELECT id,student_id,amount FROM payments WHERE request_key=$1", key).Scan(&previous.ID, &previous.StudentID, &previous.Amount)
	if err == nil {
		if previous.StudentID != p.StudentID || previous.Amount != p.Amount {
			fail(w, 409, "idempotency key reused with different payload")
			return
		}
		respond(w, 200, previous)
		return
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		a.dbError(w, err)
		return
	}
	err = tx.QueryRow(r.Context(), "INSERT INTO payments(student_id,amount,request_key) VALUES($1,$2,$3) RETURNING id", p.StudentID, p.Amount, key).Scan(&p.ID)
	if err != nil {
		a.dbError(w, err)
		return
	}
	if interrupt {
		fail(w, 503, "interrupted transaction; rolled back")
		return
	}
	if _, err = tx.Exec(r.Context(), "UPDATE students SET paid=paid+$2 WHERE id=$1", p.StudentID, p.Amount); err != nil {
		a.dbError(w, err)
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		a.dbError(w, err)
		return
	}
	respond(w, 201, p)
}

func (a *App) grade(w http.ResponseWriter, r *http.Request) {
	var in struct {
		StudentID int64  `json:"student_id"`
		Course    string `json:"course"`
		Grade     int    `json:"grade"`
	}
	if !decode(w, r, &in) {
		return
	}
	if in.StudentID <= 0 || len(strings.TrimSpace(in.Course)) == 0 || len(in.Course) > 100 || in.Grade < 0 || in.Grade > 100 {
		fail(w, 400, "invalid student, course or grade")
		return
	}
	_, err := a.DB.Exec(r.Context(), "INSERT INTO grades(student_id,course,grade) VALUES($1,$2,$3) ON CONFLICT(student_id,course) DO UPDATE SET grade=EXCLUDED.grade", in.StudentID, in.Course, in.Grade)
	if err != nil {
		a.dbError(w, err)
		return
	}
	respond(w, 200, in)
}

func (a *App) transcript(w http.ResponseWriter, r *http.Request) {
	id, err := studentID(r)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	tx, err := a.DB.Begin(r.Context())
	if err != nil {
		a.dbError(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	if _, err = tx.Exec(r.Context(), "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"); err != nil {
		a.dbError(w, err)
		return
	}
	var s Student
	if err = tx.QueryRow(r.Context(), "SELECT id,name,paid FROM students WHERE id=$1", id).Scan(&s.ID, &s.Name, &s.Paid); err != nil {
		a.dbError(w, err)
		return
	}
	rows, err := tx.Query(r.Context(), "SELECT course,grade FROM grades WHERE student_id=$1 ORDER BY course", id)
	if err != nil {
		a.dbError(w, err)
		return
	}
	grades := []map[string]any{}
	for rows.Next() {
		var course string
		var grade int
		if err := rows.Scan(&course, &grade); err != nil {
			rows.Close()
			a.dbError(w, err)
			return
		}
		grades = append(grades, map[string]any{"course": course, "grade": grade})
	}
	rows.Close()
	if rows.Err() != nil {
		a.dbError(w, rows.Err())
		return
	}
	respond(w, 200, map[string]any{"student": s, "grades": grades, "generated_at": time.Now().UTC()})
}
