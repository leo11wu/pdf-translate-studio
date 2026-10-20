"""A deterministic local provider for protocol and packaging tests, never quality evaluation."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProviderHandler(BaseHTTPRequestHandler):
    requests_seen = []

    def log_message(self, *_args):
        pass

    def respond(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.respond({"object": "list", "data": [{"id": "group-test-model"}, {"id": "another-test-model"}]})
        else:
            self.respond({"detail": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.respond({"detail": "not found"}, 404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = body["messages"][-1]["content"]
        self.requests_seen.append({"model": body.get("model"), "path": self.path, "hello": content.strip() == "Hello" or content.rstrip().endswith("Hello")})
        translation = "你好"
        if "## Here is the input:" in content:
            entries = json.loads(content.rsplit("## Here is the input:", 1)[1].strip())
            output = []
            for entry in entries:
                # Preserve engine formula/style placeholders while replacing prose.
                tags = re.findall(r"\{[^{}]*\}|</?style[^>]*>", entry["input"])
                text = "这是一份研究文档。语言学习依赖经验，研究者比较不同条件并报告结果。"
                output.append({"id": entry["id"], "output": "".join(tags) + text})
            translation = json.dumps(output, ensure_ascii=False)
        self.respond({"id": "local-test", "object": "chat.completion", "created": 0,
            "model": body.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": translation}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})


def make_provider(port=0):
    return ThreadingHTTPServer(("127.0.0.1", port), ProviderHandler)


if __name__ == "__main__":
    server = make_provider(8897)
    print("Local test provider: http://127.0.0.1:8897/v1", flush=True)
    server.serve_forever()
