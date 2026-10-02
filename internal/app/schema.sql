CREATE TABLE IF NOT EXISTS students (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    paid BIGINT NOT NULL DEFAULT 0 CHECK (paid >= 0)
);
CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    student_id BIGINT NOT NULL REFERENCES students(id),
    amount BIGINT NOT NULL CHECK (amount > 0 AND amount <= 100000000),
    request_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS grades (
    id BIGSERIAL PRIMARY KEY,
    student_id BIGINT NOT NULL REFERENCES students(id),
    course TEXT NOT NULL,
    grade INT NOT NULL CHECK (grade BETWEEN 0 AND 100),
    UNIQUE (student_id,course)
);
CREATE TABLE IF NOT EXISTS faults (
    role TEXT PRIMARY KEY,
    delay_ms INT NOT NULL DEFAULT 0,
    failures INT NOT NULL DEFAULT 0
);
INSERT INTO faults(role) VALUES ('student'),('payment'),('records') ON CONFLICT DO NOTHING;
