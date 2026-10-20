import AppKit
import WebKit
import Security
import UniformTypeIdentifiers

final class StudioDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandlerWithReply {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusLabel: NSTextField!
    private var engine: Process?
    private var stateURL: URL!
    private var sessionURL: URL?
    private var bootTimer: Timer?
    private var bootStarted = Date()
    private var checkingHealth = false
    private var terminating = false
    private var token = UUID().uuidString + UUID().uuidString
    private let profileQueue = DispatchQueue(label: "org.paperstudio.credentials")
    private let exportQueue = DispatchQueue(label: "org.paperstudio.exports")
    private lazy var providerStore = ProviderStore(
        read: { UserDefaults.standard.data(forKey: "providerProfilesV2") },
        write: { UserDefaults.standard.set($0, forKey: "providerProfilesV2") },
        legacy: { UserDefaults.standard.dictionary(forKey: "provider") },
        secrets: { account in KeychainStore(service: Bundle.main.bundleIdentifier == "org.paperstudio.qa"
            ? "org.paperstudio.qa.api-profile" : "org.paperstudio.api-profile", account: account) },
        send: { [unowned self] profile in try self.sendProfile(profile) })
    private var downloadTargets: [ObjectIdentifier: (URL, URL)] = [:]
    private lazy var supportURL: URL = {
        if let override = ProcessInfo.processInfo.environment["PDF_STUDIO_SUPPORT_DIR"] {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        let folder = Bundle.main.bundleIdentifier == "org.paperstudio.qa" ? "PaperStudio-QA" : "PaperStudio"
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(folder, isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        if let existing = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "org.paperstudio.app").first(where: {
            $0.processIdentifier != ProcessInfo.processInfo.processIdentifier
        }) {
            existing.activate(options: [.activateIgnoringOtherApps])
            NSApp.terminate(nil)
            return
        }
        makeMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = Bundle.main.bundleIdentifier == "org.paperstudio.qa" ? "纸间 QA · 模拟测试（请勿填写真实密钥）" : "纸间 · PDF 译制台"
        window.minSize = NSSize(width: 900, height: 660)
        window.delegate = self
        window.center()
        let loading = NSStackView()
        loading.orientation = .vertical
        loading.spacing = 20
        let title = NSTextField(labelWithString: "纸间 · PDF 译制台")
        title.font = NSFont.systemFont(ofSize: 27, weight: .medium)
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.startAnimation(nil)
        statusLabel = NSTextField(labelWithString: "正在启动本地译制服务…")
        statusLabel.textColor = .secondaryLabelColor
        loading.addArrangedSubview(title)
        loading.addArrangedSubview(spinner)
        loading.addArrangedSubview(statusLabel)
        loading.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(loading)
        NSLayoutConstraint.activate([
            loading.centerXAnchor.constraint(equalTo: window.contentView!.centerXAnchor),
            loading.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor),
        ])
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        do { try startEngine() } catch { showFatal(error.localizedDescription) }
    }

    private func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于纸间", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出纸间", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                                     ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit
        menu.addItem(editItem)
        let helpItem = NSMenuItem()
        let help = NSMenu(title: "帮助")
        let logs = help.addItem(withTitle: "打开日志与本地数据", action: #selector(openData), keyEquivalent: "")
        logs.target = self
        let guide = help.addItem(withTitle: "使用说明", action: #selector(openGuide), keyEquivalent: "")
        guide.target = self
        helpItem.submenu = help
        menu.addItem(helpItem)
        NSApp.mainMenu = menu
    }

    @objc private func openData() { NSWorkspace.shared.open(supportURL) }
    @objc private func openGuide() {
        if let guide = Bundle.main.url(forResource: "使用说明", withExtension: "txt") { NSWorkspace.shared.open(guide) }
    }

    private func startEngine() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: supportURL, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let runURL = supportURL.appendingPathComponent("run/\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: runURL, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        stateURL = runURL.appendingPathComponent("service.json")
        let logURL = supportURL.appendingPathComponent("engine.log")
        if let size = try? logURL.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 3 * 1024 * 1024 {
            try? fm.removeItem(at: supportURL.appendingPathComponent("engine.previous.log"))
            try? fm.moveItem(at: logURL, to: supportURL.appendingPathComponent("engine.previous.log"))
        }
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        let log = try FileHandle(forWritingTo: logURL)
        log.seekToEndOfFile()
        let process = Process()
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/StudioEngine.app/Contents/MacOS/studio-engine")
        var env = ProcessInfo.processInfo.environment
        env["PDF_STUDIO_SUPPORT_DIR"] = supportURL.path
        env["PDF_STUDIO_WEB_DIR"] = Bundle.main.resourceURL!.appendingPathComponent("web").path
        env["PDF_STUDIO_STATE_FILE"] = stateURL.path
        env["PDF_STUDIO_SESSION_TOKEN"] = token
        env["PDF_STUDIO_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env
        process.standardOutput = log
        process.standardError = log
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.terminating else { return }
                self.showFatal("本地译制服务已停止。可在“帮助”菜单中查看日志，然后重新打开 App。")
            }
        }
        engine = process
        try process.run()
        bootStarted = Date()
        bootTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.checkEngine() }
    }

    private func checkEngine() {
        guard !checkingHealth else { return }
        if Date().timeIntervalSince(bootStarted) > 120 {
            bootTimer?.invalidate()
            showFatal("本地服务启动超时。请从“帮助”菜单打开日志查看详情。")
            return
        }
        guard let data = try? Data(contentsOf: stateURL),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = state["port"] as? Int,
              let base = URL(string: "http://127.0.0.1:\(port)") else { return }
        checkingHealth = true
        var request = URLRequest(url: base.appendingPathComponent("api/health"))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.checkingHealth = false
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.bootTimer?.invalidate()
                    self.sessionURL = base
                    self.showWebView(base)
                    #if PAPERSTUDIO_VALIDATION
                    if let plan = ProcessInfo.processInfo.environment["PAPERSTUDIO_VALIDATION_PLAN"] {
                        DispatchQueue.global(qos: .utility).async {
                            do {
                                var provider = ""
                                try self.profileQueue.sync {
                                    try self.providerStore.load()
                                    provider = self.providerStore.session.base_url
                                }
                                ValidationDriver.run(planPath: plan, base: base, token: self.token, providerURL: provider,
                                                     export: { try self.exportJob($0) })
                            } catch { ValidationDriver.trace("Validation configuration error: \(error.localizedDescription)") }
                        }
                    }
                    #endif
                }
            }
        }.resume()
    }

    private func showWebView(_ base: URL) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "studioNative")
        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.allowsBackForwardNavigationGestures = false
        let cookie = HTTPCookie(properties: [.domain: "127.0.0.1", .path: "/", .name: "studio_session",
                                           .value: token, HTTPCookiePropertyKey("HttpOnly"): "TRUE",
                                           HTTPCookiePropertyKey("SameSite"): "Strict"])!
        config.websiteDataStore.httpCookieStore.setCookie(cookie) { [weak self] in
            guard let self = self else { return }
            self.window.contentView = self.webView
            self.webView.load(URLRequest(url: base))
        }
    }

    private func local(_ url: URL?) -> Bool {
        guard let url = url, let base = sessionURL else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if local(action.request.url) {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            if action.navigationType == .linkActivated, let url = action.request.url,
               ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard local(response.response.url) else { decisionHandler(.cancel); return }
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if disposition.lowercased().contains("attachment") || response.response.mimeType == "application/pdf" || !response.canShowMIMEType {
            decisionHandler(.download)
        } else { decisionHandler(.allow) }
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let failure = error as NSError
        // WebKit reports a policy interruption when a navigation becomes a
        // WKDownload. That transition is expected and the download delegate
        // owns its error handling and native save panel.
        if failure.code == NSURLErrorCancelled || (failure.domain == "WebKitErrorDomain" && failure.code == 102) { return }
        showFatal("界面载入失败：\(error.localizedDescription)")
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard local(frame.request.url) else { completionHandler(false); return }
        let alert = NSAlert()
        alert.messageText = "确认操作"
        alert.informativeText = message
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.allowedContentTypes = suggestedFilename.lowercased().hasSuffix(".txt") ? [.plainText] : [.pdf]
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = outputDirectory()
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self, response == .OK, let destination = panel.url else { completionHandler(nil); return }
            let staging = self.supportURL.appendingPathComponent("run/\(UUID().uuidString).\(destination.pathExtension)")
            self.downloadTargets[ObjectIdentifier(download)] = (staging, destination)
            completionHandler(staging)
        }
    }
    func downloadDidFinish(_ download: WKDownload) {
        guard let (staging, destination) = downloadTargets.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                _ = try FileManager.default.replaceItemAt(destination, withItemAt: staging)
            } else { try FileManager.default.moveItem(at: staging, to: destination) }
        } catch {
            let alert = NSAlert()
            alert.messageText = "保存未完成"
            alert.informativeText = "\(error.localizedDescription)\n可回到界面再次下载。"
            alert.beginSheetModal(for: window)
            try? FileManager.default.removeItem(at: staging)
        }
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let (staging, _) = downloadTargets.removeValue(forKey: ObjectIdentifier(download)) {
            try? FileManager.default.removeItem(at: staging)
        }
        if (error as NSError).code != NSURLErrorCancelled {
            let alert = NSAlert()
            alert.messageText = "下载未完成"
            alert.informativeText = error.localizedDescription
            alert.beginSheetModal(for: window)
        }
    }

    private func normalizedURL(_ raw: String) throws -> String {
        guard var parts = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(parts.scheme ?? ""), parts.host != nil,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil else {
            throw StudioError.message("请填写完整的 http(s) API 基础地址。")
        }
        while parts.path.hasSuffix("/") { parts.path.removeLast() }
        if parts.path.hasSuffix("/chat/completions") { parts.path = String(parts.path.dropLast("/chat/completions".count)) }
        if parts.path.isEmpty { parts.path = "/v1" }
        return parts.string!
    }

    private func outputDirectory() -> URL {
        if let path = UserDefaults.standard.string(forKey: "outputDirectory") {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        return FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("纸间译文", isDirectory: true)
    }

    private func outputSettings() -> [String: Any] {
        ["directory": outputDirectory().path,
         "auto_save": UserDefaults.standard.object(forKey: "autoSaveResults") as? Bool ?? true]
    }

    private func localRequest(_ route: String) throws -> URLRequest {
        guard let base = sessionURL, route.hasPrefix("/api/jobs/"), !route.contains(".."),
              let url = URL(string: route, relativeTo: base)?.absoluteURL, local(url) else {
            throw StudioError.message("无效的本地文件请求。")
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 120
        return request
    }

    private func jobSnapshot(_ jobID: String) throws -> [String: Any] {
        let request = try localRequest("/api/jobs/\(jobID)")
        let semaphore = DispatchSemaphore(value: 0)
        var result: [String: Any]?
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 200, let data = data {
                result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 125) == .success, let result = result else {
            task.cancel()
            throw StudioError.message("未能读取本地任务；请回到历史记录重试。")
        }
        return result
    }

    private func copyArtifact(_ route: String, to destination: URL) throws {
        let request = try localRequest(route)
        let semaphore = DispatchSemaphore(value: 0)
        var succeeded = false
        var failure = "未能读取本地结果文件。"
        let task = URLSession.shared.downloadTask(with: request) { temporary, response, error in
            defer { semaphore.signal() }
            if let error = error { failure = error.localizedDescription; return }
            guard (response as? HTTPURLResponse)?.statusCode == 200, let temporary = temporary else { return }
            do {
                try FileManager.default.moveItem(at: temporary, to: destination)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
                succeeded = true
            } catch { failure = error.localizedDescription }
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 125) == .success, succeeded else {
            task.cancel()
            throw StudioError.message("保存未完成：\(failure)")
        }
    }

    private func exportJob(_ jobID: String) throws -> URL {
        let snapshot = try jobSnapshot(jobID)
        guard snapshot["status"] as? String == "completed",
              let artifacts = snapshot["artifacts"] as? [[String: String]], !artifacts.isEmpty else {
            throw StudioError.message("任务尚未完成或没有可保存的文件。")
        }
        let fm = FileManager.default
        var index = UserDefaults.standard.dictionary(forKey: "exportedJobs") as? [String: String] ?? [:]
        if let path = index[jobID] {
            let folder = URL(fileURLWithPath: path, isDirectory: true)
            if artifacts.allSatisfy({ fm.fileExists(atPath: folder.appendingPathComponent($0["filename"] ?? "").path) }) {
                return folder
            }
        }
        let root = outputDirectory()
        try fm.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let name = (snapshot["filename"] as? String ?? "document.pdf")
        var stem = String(URL(fileURLWithPath: name).deletingPathExtension().lastPathComponent.prefix(90))
        while stem.utf8.count > 160 { stem.removeLast() }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HHmmss"
        let date = Date(timeIntervalSince1970: snapshot["created_at"] as? Double ?? Date().timeIntervalSince1970)
        let folderName = "\(stem)--\(formatter.string(from: date))-\(jobID.prefix(8))"
        var destination = root.appendingPathComponent(folderName, isDirectory: true)
        if fm.fileExists(atPath: destination.path) {
            destination = root.appendingPathComponent(folderName + "-" + UUID().uuidString.prefix(8), isDirectory: true)
        }
        let staging = root.appendingPathComponent(".paperstudio-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: staging, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        do {
            for artifact in artifacts {
                guard let filename = artifact["filename"], filename == URL(fileURLWithPath: filename).lastPathComponent,
                      !filename.hasPrefix("."), !filename.contains("/"), !filename.contains("\\"),
                      let route = artifact["route"],
                      ["/api/jobs/\(jobID)/download", "/api/jobs/\(jobID)/download-dual", "/api/jobs/\(jobID)/source.txt", "/api/jobs/\(jobID)/translated.txt"].contains(route) else {
                    throw StudioError.message("本地结果文件名无效。")
                }
                try copyArtifact(route, to: staging.appendingPathComponent(filename))
            }
            try fm.moveItem(at: staging, to: destination)
            index[jobID] = destination.path
            UserDefaults.standard.set(index, forKey: "exportedJobs")
            return destination
        } catch {
            try? fm.removeItem(at: staging)
            throw error
        }
    }

    private func handleFiles(_ body: [String: Any], action: String,
                             reply: @escaping (Any?, String?) -> Void) {
        if action == "outputSettings" { reply(outputSettings(), nil); return }
        if action == "chooseOutput" {
            let panel = NSOpenPanel()
            panel.title = "选择统一输出目录"
            panel.prompt = "使用此目录"
            panel.allowedContentTypes = []
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = true
            panel.allowsMultipleSelection = false
            panel.directoryURL = outputDirectory().deletingLastPathComponent()
            panel.beginSheetModal(for: window) { response in
                if response == .OK, let url = panel.url {
                    UserDefaults.standard.set(url.path, forKey: "outputDirectory")
                }
                reply(self.outputSettings(), nil)
            }
            return
        }
        if action == "setAutoSave" {
            UserDefaults.standard.set(body["enabled"] as? Bool ?? true, forKey: "autoSaveResults")
            reply(outputSettings(), nil)
            return
        }
        if action == "openOutput" {
            do {
                try FileManager.default.createDirectory(at: outputDirectory(), withIntermediateDirectories: true)
                NSWorkspace.shared.open(outputDirectory())
                reply(outputSettings(), nil)
            } catch { reply(nil, error.localizedDescription) }
            return
        }
        guard let jobID = body["job_id"] as? String,
              jobID.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else {
            reply(nil, "任务编号无效。")
            return
        }
        exportQueue.async { [weak self] in
            guard let self = self else { return }
            do {
                let folder = try self.exportJob(jobID)
                var values = self.outputSettings()
                values["folder"] = folder.path
                DispatchQueue.main.async {
                    if action == "openJobFolder" { NSWorkspace.shared.open(folder) }
                    reply(values, nil)
                }
            } catch {
                let message = "保存结果失败：\(error.localizedDescription)\n任务仍在历史记录中，可更换目录后重试。"
                DispatchQueue.main.async { reply(nil, message) }
            }
        }
    }

    private func sendProfile(_ profile: ProviderProfile) throws {
        guard let base = sessionURL else { throw StudioError.message("本地服务尚未就绪。") }
        var request = URLRequest(url: base.appendingPathComponent("api/desktop/profile"))
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(profile)
        let semaphore = DispatchSemaphore(value: 0)
        var succeeded = false
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            succeeded = (response as? HTTPURLResponse)?.statusCode == 200
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 12) == .success, succeeded else {
            task.cancel()
            throw StudioError.message("配置未能送达本地服务，请重试。")
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.frameInfo.isMainFrame, local(message.frameInfo.request.url),
              let body = message.body as? [String: Any], let action = body["action"] as? String else {
            replyHandler(nil, "无效的本地配置请求。")
            return
        }
        if ["outputSettings", "chooseOutput", "setAutoSave", "openOutput", "exportJob", "openJobFolder"].contains(action) {
            handleFiles(body, action: action, reply: replyHandler)
            return
        }
        profileQueue.async { [weak self] in
            guard let self = self else { return }
            do {
                switch action {
                case "load", "list":
                    try self.providerStore.load()
                case "select":
                    try self.providerStore.select(body["profile_id"] as? String ?? "")
                case "save":
                    guard let values = body["profile"] as? [String: Any] else { throw StudioError.message("配置内容无效。") }
                    var profile = ProviderProfile()
                    profile.id = values["id"] as? String ?? self.providerStore.session.id
                    profile.name = String((values["name"] as? String ?? "").prefix(100))
                    profile.base_url = try self.normalizedURL(values["base_url"] as? String ?? "")
                    profile.model = (values["model"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    profile.qps = max(1, min(values["qps"] as? Int ?? 8, 64))
                    profile.runtime = RuntimePreferences(values: values["runtime"] as? [String: Any] ?? [:])
                    profile.api_key = (values["api_key"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    try self.providerStore.save(profile, remember: values["remember"] as? Bool ?? true)
                case "forget", "delete":
                    try self.providerStore.forget(deleteRecord: action == "delete")
                default:
                    throw StudioError.message("不支持的配置操作。")
                }
                let result = self.providerStore.publicValues()
                DispatchQueue.main.async { replyHandler(result, nil) }
            } catch {
                let description = error.localizedDescription
                DispatchQueue.main.async { replyHandler(nil, description) }
            }
        }
    }

    private func showFatal(_ message: String) {
        bootTimer?.invalidate()
        let alert = NSAlert()
        alert.messageText = "纸间暂时无法运行"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.addButton(withTitle: "打开日志目录")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertSecondButtonReturn { self?.openData() }
            NSApp.terminate(nil)
        }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let webView = webView else { return .terminateNow }
        webView.evaluateJavaScript("document.querySelector('[data-job-active=\"true\"]') !== null") { [weak self] active, _ in
            guard let self = self else { NSApp.reply(toApplicationShouldTerminate: true); return }
            if active as? Bool == true {
                let alert = NSAlert()
                alert.messageText = "译制任务仍在运行"
                alert.informativeText = "退出会取消当前任务，已下载的模型与字体会保留。"
                alert.addButton(withTitle: "继续译制")
                alert.addButton(withTitle: "取消任务并退出")
                alert.beginSheetModal(for: self.window) { response in NSApp.reply(toApplicationShouldTerminate: response == .alertSecondButtonReturn) }
            } else { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }
    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        bootTimer?.invalidate()
        if let process = engine, process.isRunning {
            let pid = process.processIdentifier
            if getpgid(pid) == pid { kill(-pid, SIGTERM) } else { process.terminate() }
            for _ in 0..<20 {
                if !process.isRunning { break }
                Thread.sleep(forTimeInterval: 0.1)
            }
            if getpgid(pid) == pid { kill(-pid, SIGKILL) }
            else if process.isRunning { kill(pid, SIGKILL) }
        }
        if let state = stateURL { try? FileManager.default.removeItem(at: state.deletingLastPathComponent()) }
    }
}

@main
struct StudioMain {
    static func main() {
if CommandLine.arguments.contains("--self-test-keychain") {
    let store = KeychainStore(service: "org.paperstudio.test.\(UUID().uuidString)")
    do {
        let value = Data("paperstudio-keychain-test".utf8)
        try store.save(value)
        guard try store.read() == value else { throw StudioError.message("read mismatch") }
        try store.save(Data("updated".utf8))
        guard try store.read() == Data("updated".utf8) else { throw StudioError.message("update mismatch") }
        try store.delete()
        guard try store.read() == nil else { throw StudioError.message("delete mismatch") }
        print("Keychain create/read/update/delete: PASS")
    } catch {
        try? store.delete()
        fputs("Keychain test failed: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
} else {
    let app = NSApplication.shared
    let delegate = StudioDelegate()
    app.delegate = delegate
    app.run()
}
    }
}
