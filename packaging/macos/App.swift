// KareSanScape の Mac アプリ（旧名 OntoMap）。
// .venv の ontomap-app を起こし、その画面を WKWebView で出すだけの殻。
// マップと計算結果はすべて ontomap-app 側 (~/OntoMap) にある。この殻は状態を持たない。
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    let appName = "KareSanScape"
    var window: NSWindow!
    var webView: WKWebView!
    var engine: Process?
    var log: FileHandle?
    var poll: Timer?
    var readyURL: URL?
    var baseURL: URL?
    var startedAt = Date()
    var quitting = false
    let fm = FileManager.default

    /// Info.plist へ書き込んだ ontomap-app コマンドの絶対パス
    var command: URL? {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "LXCommand") as? String, !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path)
    }

    /// 保存先 (ontomap-app と同じ規則: ONTOMAP_HOME、無ければ ~/OntoMap)
    var dataDirectory: URL {
        if let raw = ProcessInfo.processInfo.environment["ONTOMAP_HOME"], !raw.isEmpty {
            return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath, isDirectory: true)
        }
        return fm.homeDirectoryForCurrentUser.appendingPathComponent("OntoMap", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        let config = WKWebViewConfiguration()
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.underPageBackgroundColor = NSColor(red: 0.973, green: 0.969, blue: 0.957, alpha: 1)   // 生成り

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = appName
        window.minSize = NSSize(width: 960, height: 640)
        window.contentView = webView
        window.setFrameAutosaveName("ontomap-main")
        if !window.setFrameUsingName("ontomap-main") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startEngine()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        stopEngine()
    }

    // MARK: メニュー
    func buildMenu() {
        let bar = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "\(appName) について", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "ブラウザで開く", action: #selector(openInBrowser), keyEquivalent: "")
        appMenu.addItem(withTitle: "保存先のフォルダを開く", action: #selector(openData), keyEquivalent: "")
        appMenu.addItem(withTitle: "ログを開く", action: #selector(openLog), keyEquivalent: "")
        appMenu.addItem(withTitle: "再読み込み", action: #selector(reloadPage), keyEquivalent: "r")
        appMenu.addItem(withTitle: "再起動", action: #selector(restart), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "\(appName) を隠す", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "\(appName) を終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        bar.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "編集")
        editMenu.addItem(withTitle: "元に戻す", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "やり直す", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "切り取り", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "コピー", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "貼り付け", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "すべて選択", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        bar.addItem(editItem)

        let winItem = NSMenuItem()
        let winMenu = NSMenu(title: "ウインドウ")
        winMenu.addItem(withTitle: "しまう", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "拡大／縮小", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winMenu.addItem(withTitle: "閉じる", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = winMenu
        bar.addItem(winItem)
        NSApp.mainMenu = bar
        NSApp.windowsMenu = winMenu
    }

    // MARK: ontomap-app の起動と停止
    func startEngine() {
        stopEngine()
        baseURL = nil
        splash("起動しています…")
        do {
            guard let cmd = command, fm.isExecutableFile(atPath: cmd.path) else {
                throw fail("ontomap-app が見つかりません。ontomap のフォルダを動かした場合は、そのフォルダで packaging/macos/build_app.sh を実行し直してください。")
            }
            try fm.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
            let ready = dataDirectory.appendingPathComponent("ready-\(UUID().uuidString).json")
            readyURL = ready
            let logFile = dataDirectory.appendingPathComponent("app.log")
            if !fm.fileExists(atPath: logFile.path) { fm.createFile(atPath: logFile.path, contents: nil) }
            let handle = try FileHandle(forWritingTo: logFile)
            handle.seekToEndOfFile()
            log = handle

            let child = Process()
            child.executableURL = cmd
            child.arguments = ["serve", "--no-browser", "--ready-file", ready.path,
                               "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
            var env = ProcessInfo.processInfo.environment
            env.removeValue(forKey: "PYTHONHOME")
            env.removeValue(forKey: "PYTHONPATH")
            child.environment = env
            child.currentDirectoryURL = dataDirectory
            child.standardOutput = handle
            child.standardError = handle
            child.terminationHandler = { [weak self] task in
                DispatchQueue.main.async {
                    guard let self = self, !self.quitting, self.engine === task else { return }
                    self.poll?.invalidate(); self.poll = nil
                    self.showError("KareSanScape の処理が止まりました。メニューの「ログを開く」で app.log を確かめてください。")
                }
            }
            engine = child
            try child.run()
            startedAt = Date()
            poll = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.checkReady() }
        } catch {
            showError(error.localizedDescription)
        }
    }

    func checkReady() {
        guard let ready = readyURL else { return }
        if let data = try? Data(contentsOf: ready),
           let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let value = object["url"] as? String, let url = URL(string: value),
           url.scheme == "http", url.host == "127.0.0.1", (url.port ?? 0) > 0 {
            poll?.invalidate(); poll = nil
            baseURL = url
            try? fm.removeItem(at: ready)
            readyURL = nil
            webView.load(URLRequest(url: url))
            return
        }
        let waited = Date().timeIntervalSince(startedAt)
        if waited > 10, waited < 10.2 { splash("起動しています…\n初回は OS の検査で時間がかかることがあります") }
        if waited > 90 {
            stopEngine()
            showError("90 秒以内に起動できませんでした。メニューの「ログを開く」で app.log を確かめてください。")
        }
    }

    func stopEngine() {
        poll?.invalidate(); poll = nil
        if let task = engine {
            engine = nil
            task.terminationHandler = nil
            if task.isRunning {
                task.terminate()
                let deadline = Date().addingTimeInterval(3)
                while task.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.02) }
                if task.isRunning { kill(task.processIdentifier, SIGKILL) }
            }
        }
        try? log?.close(); log = nil
        if let ready = readyURL { try? fm.removeItem(at: ready); readyURL = nil }
    }

    // MARK: 画面の制限 (外のページはブラウザで開く)
    func isLocal(_ url: URL?) -> Bool {
        guard let url = url, let base = baseURL else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        if navigationAction.shouldPerformDownload && isLocal(url) {
            decisionHandler(.download)
            return
        }
        if url == nil || isLocal(url) || url?.scheme == "about" {
            decisionHandler(.allow)
            return
        }
        if let external = url, external.scheme == "http" || external.scheme == "https" {
            NSWorkspace.shared.open(external)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        // エクスポート (JSON/CSV) は Content-Disposition: attachment で返るので保存ダイアログへ
        if let http = navigationResponse.response as? HTTPURLResponse,
           let disp = http.value(forHTTPHeaderField: "Content-Disposition"), disp.hasPrefix("attachment") {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // target=_blank (エクスポートなど) は同じ画面で開き、外部 URL はブラウザへ
        if let url = navigationAction.request.url {
            if isLocal(url) { webView.load(URLRequest(url: url)) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    // MARK: 確認・保存 (WKWebView は既定ではどれも出さない)
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "やめる")
        alert.beginSheetModal(for: window) { response in completionHandler(response == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "やめる")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.beginSheetModal(for: window) { result in
            guard result == .OK, let url = panel.url else { completionHandler(nil); return }
            try? FileManager.default.removeItem(at: url)
            completionHandler(url)
        }
    }

    // MARK: 操作
    @objc func reloadPage() { if baseURL != nil { webView.reload() } else { startEngine() } }
    @objc func restart() { startEngine() }
    @objc func openData() { NSWorkspace.shared.open(dataDirectory) }
    @objc func openLog() { NSWorkspace.shared.open(dataDirectory.appendingPathComponent("app.log")) }
    @objc func openInBrowser() { if let url = baseURL { NSWorkspace.shared.open(url) } }

    @objc func showAbout() {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        let alert = NSAlert()
        alert.messageText = "\(appName) \(version)"
        alert.informativeText = """
        特許分析者向けの分類辞書構築アプリ。資料を登録し、MCP 接続した LLM の辞書案をレビューして、確定版の辞書とチャット用プロンプトを作ります。
        保存先: \(dataDirectory.path)
        画面との通信はこの Mac の中 (127.0.0.1) だけです。
        """
        alert.runModal()
    }

    func splash(_ message: String) {
        var img = ""
        if let url = Bundle.main.url(forResource: "splash_mark", withExtension: "png"), let data = try? Data(contentsOf: url) {
            img = "<img src=\"data:image/png;base64,\(data.base64EncodedString())\" style=\"width:96px;height:96px;display:block;margin:0 auto 20px\">"
        }
        let html = """
        <html lang="ja"><head><meta charset="utf-8"><style>body{background:#F8F7F4;color:#24282B;font:15px -apple-system,"Hiragino Sans";display:flex;align-items:center;justify-content:center;height:100vh;margin:0}div{text-align:center}h2{font-weight:700;font-size:34px;letter-spacing:-.02em;margin:0 0 8px;color:#172B3A}h2 span{color:#B94B42}small{color:#66727C;letter-spacing:.1em;font-size:12px}em{display:block;color:#C9A96B;font-style:normal;font-size:10px;letter-spacing:.2em;margin-top:4px}p{white-space:pre-line;color:#66727C;margin-top:32px}</style></head>
        <body><div>\(img)<h2>Le<span>X</span>ify</h2><small>知をつなぎ、未来をひらく</small><em>FROM KNOWLEDGE TO IMPACT</em><p>\(message)</p></div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    func fail(_ message: String) -> NSError {
        NSError(domain: "KareSanScape", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "起動できませんでした"
        alert.informativeText = message
        alert.addButton(withTitle: "再起動")
        alert.addButton(withTitle: "ログを開く")
        alert.addButton(withTitle: "終了")
        switch alert.runModal() {
        case .alertFirstButtonReturn: startEngine()
        case .alertSecondButtonReturn: openLog()
        default: NSApp.terminate(nil)
        }
    }
}

let delegate = AppDelegate()
let application = NSApplication.shared
application.delegate = delegate
application.run()
