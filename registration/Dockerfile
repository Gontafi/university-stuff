# static Go binary, built with: CGO_ENABLED=0 go build -trimpath -o registration .
FROM scratch
COPY registration /registration
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/registration"]
