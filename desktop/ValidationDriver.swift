#if PAPERSTUDIO_VALIDATION
import Foundation

// Compiled only into a local validation copy, never the distributed executable.
enum ValidationDriver {
    static func trace(_ value: String) { FileHandle.standardOutput.write(Data((value + "\n").utf8)) }
    static func run(planPath: String, base: URL, token: String, providerURL: String,
                    export: (String) throws -> URL) {
        var results: [[String: Any]] = []
        var output: URL?
        func request(_ route: String, method: String = "GET", body: Data? = nil, contentType: String? = nil, retries: Int = 2) throws -> [String: Any] {
            var req = URLRequest(url: base.appendingPathComponent(route))
            req.httpMethod = method
            req.timeoutInterval = 60
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            if let contentType = contentType { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
            req.httpBody = body
            let semaphore = DispatchSemaphore(value: 0)
            var payload: Data?
            var status = 0
            var errorCode = 0
            URLSession.shared.dataTask(with: req) { data, response, error in
                payload = data; status = (response as? HTTPURLResponse)?.statusCode ?? 0
                errorCode = (error as NSError?)?.code ?? 0
                semaphore.signal()
            }.resume()
            guard semaphore.wait(timeout: .now() + 65) == .success, let data = payload,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                if method == "GET" && retries > 0 {
                    Thread.sleep(forTimeInterval: 0.5)
                    return try request(route, retries: retries - 1)
                }
                throw StudioError.message("本机验证连接未返回 JSON（HTTP \(status)，连接代码 \(errorCode)）。")
            }
            guard (200..<300).contains(status) else { throw StudioError.message("验证接口返回 HTTP \(status)：\(json["detail"] ?? "请求失败")") }
            return json
        }
        func persist() throws {
            guard let target = output else { return }
            let value: [String: Any] = ["results": results, "updated_at": Date().timeIntervalSince1970]
            try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]).write(to: target, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
        }
        do {
            let plan = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: planPath))) as! [String: Any]
            output = URL(fileURLWithPath: plan["output"] as! String)
            let cases = plan["cases"] as! [[String: Any]]
            for item in cases {
                let label = item["label"] as! String
                trace("Validation starting: \(label)")
                let source = URL(fileURLWithPath: item["file"] as! String)
                let allowed = Set(["model", "qps", "pipeline_mode", "concurrency_mode", "max_in_flight", "request_rate", "batch_tokens", "reasoning_policy", "generate_dual", "export_texts"])
                var fields = (item["data"] as? [String: String] ?? [:]).filter { allowed.contains($0.key) }
                fields["api_base_url"] = providerURL
                fields["use_cache"] = "false"
                let boundary = "PaperStudioValidation\(UUID().uuidString)"
                var body = Data()
                for (key, value) in fields.sorted(by: { $0.key < $1.key }) {
                    body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n".utf8))
                }
                body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(source.lastPathComponent)\"\r\nContent-Type: application/pdf\r\n\r\n".utf8))
                body.append(try Data(contentsOf: source))
                body.append(Data("\r\n--\(boundary)--\r\n".utf8))
                var job = try request("api/jobs", method: "POST", body: body, contentType: "multipart/form-data; boundary=\(boundary)")
                let id = job["id"] as! String
                let deadline = Date().addingTimeInterval(1800)
                while ["queued", "running", "cancelling"].contains(job["status"] as? String ?? "") && Date() < deadline {
                    Thread.sleep(forTimeInterval: 1)
                    job = try request("api/jobs/\(id)")
                }
                job["validation_label"] = label
                if item["export"] as? Bool == true && job["status"] as? String == "completed" {
                    job["exported_to"] = try export(id).path
                }
                results.append(job)
                try persist()
                trace("Validation \(label): \(job["status"] ?? "unknown") · \(job["elapsed_seconds"] ?? "unknown") seconds")
            }
        } catch {
            results.append(["validation_error": error.localizedDescription])
            try? persist()
            trace("Validation stopped: \(error.localizedDescription)")
        }
    }
}
#endif
