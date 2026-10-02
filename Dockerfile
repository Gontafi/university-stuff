FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -o /server ./cmd/server

FROM alpine:3.23
RUN adduser -D -u 10001 university
WORKDIR /app
COPY --from=build /server /app/server
COPY web /app/web
USER university
EXPOSE 8080
ENTRYPOINT ["/app/server"]
