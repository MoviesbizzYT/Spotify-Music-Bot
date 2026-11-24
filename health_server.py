from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Spotify Music Bot is running!")

    def log_message(self, format, *args):
        pass

server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
print("[health] server running on port 8080", flush=True)
server.serve_forever()
