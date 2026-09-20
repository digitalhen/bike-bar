import SwiftUI
import AppKit
import Combine
import UserNotifications
import ServiceManagement
import Sparkle

// MARK: - Config
let apiPort = 8099
let apiBase = "http://127.0.0.1:\(apiPort)"
// Notify on every event EXCEPT internal plumbing (these would just be noise:
// per-delivery webhook logs and the poller's own error/crash bookkeeping).
let silentEvents: Set<String> = [
    "webhook.delivery", "poll.error", "poll.crash",
]

// MARK: - Embedded backend
/// Spawns and supervises the bundled Python backend (`Resources/backend/boschflowd`).
/// When no bundled binary is present (dev builds run straight from `build/`), it
/// stays out of the way and assumes a server is already running — so you can keep
/// iterating with `uvicorn app.main:app` by hand.
final class BackendController {
    private var process: Process?

    /// ~/Library/Application Support/Bike Bar — a writable home for tokens,
    /// event log, poller state (the .app bundle itself is read-only). Migrates a
    /// folder from either earlier name, so a signed-in user stays signed in.
    static var dataDir: URL {
        let fm = FileManager.default
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = base.appendingPathComponent("Bike Bar", isDirectory: true)
        if !fm.fileExists(atPath: dir.path) {
            for name in ["Bosch Bar", "Bosch Flow"] {   // newest legacy name first
                let legacy = base.appendingPathComponent(name, isDirectory: true)
                if fm.fileExists(atPath: legacy.path) {
                    try? fm.moveItem(at: legacy, to: dir)
                    break
                }
            }
        }
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private var bundledBinary: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let bin = res.appendingPathComponent("backend/boschflowd", isDirectory: false)
        return FileManager.default.isExecutableFile(atPath: bin.path) ? bin : nil
    }

    /// Start the backend if we ship one. No-op (returns true) in dev.
    func start() {
        guard process == nil, let bin = bundledBinary else { return }
        let p = Process()
        p.executableURL = bin
        p.currentDirectoryURL = bin.deletingLastPathComponent()
        var env = ProcessInfo.processInfo.environment
        env["BOSCH_FLOW_DATA_DIR"] = Self.dataDir.path
        env["BOSCH_FLOW_HOST"] = "127.0.0.1"
        env["BOSCH_FLOW_PORT"] = String(apiPort)
        p.environment = env
        // Route the child's logs to a file so crashes are diagnosable.
        let log = Self.dataDir.appendingPathComponent("backend.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: log) {
            p.standardOutput = handle
            p.standardError = handle
        }
        do { try p.run(); process = p } catch { NSLog("backend failed to start: \(error)") }
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    /// Poll `/` until the server answers 200 (or we give up).
    func waitUntilHealthy(timeout: TimeInterval = 20) async {
        let deadline = Date().addingTimeInterval(timeout)
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 2
        let session = URLSession(configuration: cfg)
        while Date() < deadline {
            if let url = URL(string: apiBase + "/api/auth/status"),
               let (_, resp) = try? await session.data(from: url),
               (resp as? HTTPURLResponse)?.statusCode == 200 { return }
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
    }
}

// MARK: - Auto-update (Sparkle)
/// Wraps Sparkle's updater. Background checks run automatically (SUEnableAutomaticChecks
/// in Info.plist); "Check for Updates…" drives a manual check. Feed + EdDSA key are in
/// Info.plist (SUFeedURL / SUPublicEDKey); updates ship as notarized zips on GitHub Releases.
@MainActor
final class UpdaterController: ObservableObject {
    static let shared = UpdaterController()
    private let controller: SPUStandardUpdaterController
    @Published var canCheck = false

    private init() {
        controller = SPUStandardUpdaterController(startingUpdater: true,
                                                  updaterDelegate: nil, userDriverDelegate: nil)
        controller.updater.publisher(for: \.canCheckForUpdates).assign(to: &$canCheck)
    }

    func checkForUpdates() { controller.checkForUpdates(nil) }
}

// MARK: - Launch at Login
enum LoginItem {
    static var isEnabled: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }
    static func set(_ on: Bool) {
        guard #available(macOS 13.0, *) else { return }
        do {
            if on { try SMAppService.mainApp.register() }
            else  { try SMAppService.mainApp.unregister() }
        } catch { NSLog("login item toggle failed: \(error)") }
    }
}

// MARK: - Models
// Missing flags are unknown, not false. A full pack does not prove why charging stopped.
func chargingStatus(connected: Bool?, charging: Bool?, level: Int?) -> String {
    switch (connected, charging) {
    case (false?, true?): return "Charging status unclear"
    case (_, true?): return "Charging"
    case (true?, false?):
        return level == 100 ? "Plugged in · Fully charged" : "Plugged in · Not charging"
    case (true?, nil): return "Plugged in · Charging status unknown"
    case (false?, _): return "Unplugged"
    case (nil, false?): return "Not charging · Connection unknown"
    case (nil, nil): return "Charging status unknown"
    }
}

struct Bike: Decodable { let id: String; let brand: String?; let drive_unit: String? }
struct ChargeSchedule: Decodable, Identifiable {
    let id: String; let event: String; let at: String; let repeat_: String
    enum CodingKeys: String, CodingKey { case id, event, at, repeat_ = "repeat" }
}
struct WebhookSub: Decodable { let id: String; let events: [String] }
struct RangeMode: Decodable { let mode: String?; let range_km: Double? }
struct Battery: Decodable {
    let level_percent: Int?
    let is_charging: Bool?
    let charger_connected: Bool?
    let total_capacity_wh: Double?
    let charge_cycles: Double?
    let odometer_km: Double?
    let range_per_mode: [RangeMode]?
    let last_update: String?
    let live: Bool?
    let remaining_charging_time: Double?   // minutes to full (while charging)
}
struct Ride: Decodable {
    let id: String; let start: String?; let distance_km: Double?
    let avg_speed_kmh: Double?; let has_gps: Bool?
}

// MARK: - Store
@MainActor
final class BikeStore: ObservableObject {
    @Published var bikeName = "Bike Bar"
    @Published var battery: Battery?
    @Published var rides: [Ride] = []
    @Published var serverUp = false
    @Published var lastError: String?
    @Published var chargeEta: Date?   // when charging, anchored full-charge time
    @Published var chargeEtaEstimated = false  // true when we computed it (Bosch gave no value)
    private var chargeSamples: [(at: Date, level: Int)] = []  // SoC over time, for the estimate

    // login / token state
    @Published var loggedIn = false
    @Published var loginUser: String?
    @Published var loginStatus: String?   // transient hint shown while logging in
    @Published var loginInProgress = false // a sign-in is underway → offer the paste fallback

    // shared units pref (metric/imperial), synced with the dashboard via /api/prefs
    @Published var useImperial = UserDefaults.standard.bool(forKey: "useImperial")

    // Charge triggers. The app can't switch mains itself — it raises an event and
    // whatever the user has wired up (a smart plug, Home Assistant) does the work.
    // So the controls stay hidden unless a webhook actually subscribes to them.
    @Published var subscribedTriggers: Set<String> = []
    @Published var chargeSchedules: [ChargeSchedule] = []
    @Published var triggerStatus: String?
    @Published var chargeTarget = 80   // ceiling for the current charge, from /api/prefs
    var canStart: Bool { subscribedTriggers.contains("charge.requested") }
    var canStop: Bool { subscribedTriggers.contains("charge.stopped") }
    var chargeTriggerAvailable: Bool { canStart || canStop }

    private var bikeID: String?
    private var pendingState: String?     // OAuth `state` for the in-flight login
    private var loginPollTask: Task<Void, Never>?
    private var lastEventSeen: Double = Date().timeIntervalSince1970
    private var baselined = false

    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        c.timeoutIntervalForRequest = 12
        return URLSession(configuration: c)
    }()

    private func get(_ path: String) async -> Data? {
        guard let url = URL(string: apiBase + path) else { return nil }
        do {
            let (data, resp) = try await session.data(from: url)
            guard let h = resp as? HTTPURLResponse, h.statusCode == 200 else { return nil }
            return data
        } catch { return nil }
    }

    /// Send JSON (POST by default), returning (statusCode, body) — nil on transport failure.
    @discardableResult
    private func post(_ path: String, _ body: [String: Any], method: String = "POST") async -> (Int, Data)? {
        guard let url = URL(string: apiBase + path) else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, resp) = try await session.data(for: req)
            return ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
        } catch { return nil }
    }

    // MARK: Units (shared with the dashboard)

    private func applyImperial(_ imp: Bool) {
        guard imp != useImperial else { return }
        useImperial = imp
        UserDefaults.standard.set(imp, forKey: "useImperial")
    }

    // MARK: Charge triggers

    /// Only surface the charge controls if something is listening for them.
    func loadChargeTriggers() async {
        if let d = await get("/api/webhooks"),
           let subs = try? JSONDecoder().decode([WebhookSub].self, from: d) {
            var found: Set<String> = []
            for sub in subs {
                if sub.events.contains("*") {
                    found.formUnion(["charge.requested", "charge.stopped"])
                } else {
                    found.formUnion(sub.events.filter { $0.hasPrefix("charge.") })
                }
            }
            subscribedTriggers = found
        } else {
            subscribedTriggers = []
        }
        guard chargeTriggerAvailable else { chargeSchedules = []; return }
        if let d = await get("/api/schedules"),
           let items = try? JSONDecoder().decode([ChargeSchedule].self, from: d) {
            chargeSchedules = items
        }
    }

    func trigger(_ event: String, target: Int? = nil) async {
        var body: [String: Any] = ["event": event, "data": ["source": "menubar"]]
        if let target { body["target"] = target }
        let r = await post("/api/trigger", body)
        guard let (code, data) = r, code == 200,
              let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            triggerStatus = "Couldn't reach the backend"; return
        }
        let n = (j["delivered"] as? Int) ?? 0
        let ok = (j["ok"] as? Bool) ?? false
        if let t = j["target"] as? Int { chargeTarget = t }
        let verb = event == "charge.stopped" ? "Stop requested"
            : "Charging to \(target ?? chargeTarget)%"
        triggerStatus = n == 0 ? "No webhook is listening"
            : (ok ? verb : "Sent, but \(n == 1 ? "the" : "a") webhook failed")
        Task { try? await Task.sleep(nanoseconds: 4_000_000_000); triggerStatus = nil }
    }

    func scheduleCharging(event: String, at time: Date, daily: Bool) async {
        let fmt = DateFormatter(); fmt.dateFormat = "HH:mm"
        let body: [String: Any] = ["event": event,
                                   "at": fmt.string(from: time),
                                   "repeat": daily ? "daily" : "once"]
        guard let (code, _) = await post("/api/schedules", body), code == 201 else {
            triggerStatus = "Couldn't save the schedule"; return
        }
        triggerStatus = "Scheduled for \(fmt.string(from: time))"
        await loadChargeTriggers()
        Task { try? await Task.sleep(nanoseconds: 4_000_000_000); triggerStatus = nil }
    }

    func deleteSchedule(_ id: String) async {
        _ = await post("/api/schedules/\(id)", [:], method: "DELETE")
        await loadChargeTriggers()
    }

    /// Pull the shared units pref from the server (reflects a change made on the web).
    func loadUnits() async {
        guard let d = await get("/api/prefs"),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let units = j["units"] as? String else { return }
        if let t = j["charge_target"] as? Int { chargeTarget = t }
        applyImperial(units == "imperial")
    }

    /// Flip units and write through to the server so the dashboard follows.
    func setImperial(_ imp: Bool) async {
        applyImperial(imp)
        await post("/api/prefs", ["units": imp ? "imperial" : "metric"], method: "PUT")
    }

    // MARK: Login / token

    /// Read who (if anyone) is currently logged in.
    func checkAuthStatus() async {
        guard let d = await get("/api/auth/status"),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        loggedIn = (j["logged_in"] as? Bool) ?? false
        loginUser = j["user"] as? String
    }

    /// Kick off a login: ask the server for the Bosch auth URL, open it in the
    /// browser, and start polling for completion. The redirect comes back via
    /// the onebikeapp-ios:// URL scheme (auto-capture) or the paste fallback.
    func beginLogin() async {
        loginInProgress = true
        loginStatus = "Opening Bosch login…"
        guard let d = await get("/api/auth/login"),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let urlStr = j["auth_url"] as? String, let url = URL(string: urlStr) else {
            loginStatus = "Couldn't start login — is the server running?"
            return
        }
        pendingState = j["state"] as? String
        NSWorkspace.shared.open(url)
        loginStatus = "Waiting for Bosch login in your browser…"
        startLoginPolling()
    }

    /// Handle the onebikeapp-ios:// deep link macOS hands back after login.
    func handleRedirect(_ url: URL) async {
        loginStatus = "Completing login…"
        let (code, _) = await post("/api/auth/redirect",
                                   ["url": url.absoluteString, "state": pendingState as Any]) ?? (0, Data())
        await finishLogin(ok: code == 200, failNote: "Login was rejected — try again.")
    }

    /// Paste-fallback: the user pasted a bare code or the full redirect URL.
    func submitPastedCode(_ raw: String) async {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        loginStatus = "Completing login…"
        let (code, _) = await post("/api/auth/callback",
                                   ["code": trimmed, "state": pendingState as Any]) ?? (0, Data())
        await finishLogin(ok: code == 200, failNote: "That code didn't work — try again.")
    }

    private func finishLogin(ok: Bool, failNote: String) async {
        loginPollTask?.cancel(); loginPollTask = nil
        if ok {
            pendingState = nil
            loginInProgress = false
            bikeID = nil            // re-resolve against the new account
            loginStatus = "Signed in ✓"
            await checkAuthStatus()
            await refresh()
        } else {
            loginStatus = failNote
        }
    }

    /// Poll /api/auth/status until a background auto-capture lands (or we give up).
    private func startLoginPolling() {
        loginPollTask?.cancel()
        loginPollTask = Task { [weak self] in
            for _ in 0..<90 {                     // ~3 min at 2s
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { return }
                guard let self else { return }
                if let d = await self.get("/api/auth/status"),
                   let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   let last = j["last"] as? [String: Any], (last["ok"] as? Bool) == true {
                    await self.finishLogin(ok: true, failNote: "")
                    return
                }
            }
            self?.loginStatus = "Login timed out. Reopen and try again, or paste the code."
        }
    }

    func refresh() async {
        // Is the server even up? auth/status is unauthenticated and cheap.
        guard let sd = await get("/api/auth/status"),
              let sj = try? JSONSerialization.jsonObject(with: sd) as? [String: Any] else {
            serverUp = false
            lastError = "Can't reach \(apiBase) — starting the backend…"
            return
        }
        serverUp = true
        loggedIn = (sj["logged_in"] as? Bool) ?? false
        loginUser = sj["user"] as? String
        await loadUnits()   // keep units in sync with the dashboard
        await loadChargeTriggers()
        guard loggedIn else {
            lastError = "Not logged in — choose “Log in / Update token”."
            return
        }
        lastError = nil

        // resolve bike id once
        if bikeID == nil {
            if let d = await get("/api/bikes"),
               let bikes = try? JSONDecoder().decode([Bike].self, from: d), let b = bikes.first {
                bikeID = b.id
                bikeName = "\(b.brand ?? "eBike") \(b.drive_unit ?? "")".trimmingCharacters(in: .whitespaces)
            } else {
                lastError = "Logged in, but no bikes returned yet."
                return
            }
        }
        guard let id = bikeID else { return }

        if let d = await get("/api/bikes/\(id)/battery"),
           let bat = try? JSONDecoder().decode(Battery.self, from: d) {
            battery = bat; serverUp = true; lastError = nil
            updateChargeEta(bat)
        }
        if let d = await get("/api/bikes/\(id)/rides?gps=1"),
           let r = try? JSONDecoder().decode([Ride].self, from: d) {
            rides = Array(r.prefix(5))
        }
        await checkEvents()
    }

    /// Poll the server event log and surface new events as native notifications.
    private func checkEvents() async {
        guard let d = await get("/api/events?limit=50"),
              let arr = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] else { return }
        // events come newest-first
        let events = arr.compactMap { e -> (String, Double, [String: Any])? in
            guard let ev = e["event"] as? String, let at = e["at"] as? Double else { return nil }
            return (ev, at, e["data"] as? [String: Any] ?? [:])
        }
        if !baselined {                    // first run: don't replay history
            lastEventSeen = events.map { $0.1 }.max() ?? lastEventSeen
            baselined = true
            return
        }
        let fresh = events.filter { $0.1 > lastEventSeen && !silentEvents.contains($0.0) }
        for (ev, _, data) in fresh.reversed() {   // oldest first
            Notifier.post(for: ev, data: data, bike: bikeName)
        }
        if let newest = events.map({ $0.1 }).max() { lastEventSeen = max(lastEventSeen, newest) }
    }

    /// Prefer Bosch's own "minutes to full"; when it's absent (often for a while
    /// after plugging in), estimate from the observed charge rate and flag it (~).
    private func updateChargeEta(_ bat: Battery) {
        guard bat.is_charging ?? false else {
            chargeEta = nil; chargeEtaEstimated = false; chargeSamples.removeAll(); return
        }
        if let mins = bat.remaining_charging_time, mins > 0 {   // real value wins
            chargeEta = Date().addingTimeInterval(mins * 60)
            chargeEtaEstimated = false
            chargeSamples.removeAll()
            return
        }
        guard let lvl = bat.level_percent, lvl < 100 else { chargeEta = nil; return }
        let now = Date()
        if let last = chargeSamples.last, lvl < last.level { chargeSamples.removeAll() }  // reset on discharge
        if chargeSamples.isEmpty || chargeSamples.last?.level != lvl {
            chargeSamples.append((now, lvl))
        }
        chargeSamples.removeAll { now.timeIntervalSince($0.at) > 45 * 60 }   // ~45-min window
        if let mins = estimatedMinutesToFull(currentLevel: lvl) {
            chargeEta = now.addingTimeInterval(mins * 60)
            chargeEtaEstimated = true
        } else {
            chargeEta = nil   // not enough data yet — countdown appears once SoC ticks up
        }
    }

    private func estimatedMinutesToFull(currentLevel lvl: Int) -> Double? {
        guard let first = chargeSamples.first, let last = chargeSamples.last else { return nil }
        let dLevel = Double(last.level - first.level)
        let dMin = last.at.timeIntervalSince(first.at) / 60
        guard dLevel >= 1, dMin >= 0.5 else { return nil }   // need a measurable rate
        let rate = dLevel / dMin                              // % per minute
        return rate > 0 ? Double(100 - lvl) / rate : nil
    }

    // menu bar title — shows a live countdown while charging
    var menuTitle: String {
        guard serverUp, let b = battery, let lvl = b.level_percent else { return "—" }
        if (b.is_charging ?? false), let eta = chargeEta {
            let tilde = chargeEtaEstimated ? "~" : ""
            return "⚡︎\(lvl)% · \(tilde)\(fmtCountdown(eta.timeIntervalSinceNow))"
        }
        let bolt = (b.is_charging ?? false) ? "⚡︎" : ""
        return "\(bolt)\(lvl)%"
    }
}

func fmtCountdown(_ secs: TimeInterval) -> String {
    let m = max(0, Int(secs / 60))
    return m >= 60 ? "\(m/60)h\(m%60)m" : "\(m)m"
}

// MARK: - Notifications
enum Notifier {
    static func requestAuth() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    static func message(for ev: String, data: [String: Any], bike: String) -> (String, String, String)? {
        let lvl = data["level"] as? Int ?? Int(data["level"] as? Double ?? -1)
        switch ev {
        case "battery.full": return ("🔋 Battery fully charged", "\(bike) is at 100%", "default")
        case "battery.low": return ("🪫 Battery low — \(lvl)%", "\(bike) needs a charge", "default")
        case "battery.charging_started": return ("⚡ Charging started", "\(bike) at \(lvl)%", "default")
        case "battery.charging_stopped": return ("🔌 Charging stopped", "\(bike) at \(lvl)%", "default")
        case "charger.connected": return ("🔌 Charger connected", bike, "default")
        case "charger.disconnected": return ("🔌 Charger unplugged", bike, "default")
        case "ride.completed":
            let km = data["distance_km"] as? Double ?? 0
            return ("🚲 Ride logged", String(format: "%.1f km on %@", km, bike), "default")
        case "firmware.changed":
            let comp = data["component"] as? String ?? "component"
            let to = data["to"] as? String ?? ""
            return ("🔧 \(comp) updated", "→ \(to)", "default")
        default:
            // Any other (e.g. newly added) event still notifies — humanize the type.
            let name = ev.replacingOccurrences(of: "_", with: " ")
                .replacingOccurrences(of: ".", with: " · ")
            let body = lvl >= 0 ? "\(bike) at \(lvl)%" : bike
            return ("🔔 \(name)", body, "default")
        }
    }

    static func post(for ev: String, data: [String: Any], bike: String) {
        guard let (title, body, _) = message(for: ev, data: data, bike: bike) else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = UNNotificationSound(named: UNNotificationSoundName("bikebell.aiff"))
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

}

// MARK: - Popover UI
struct DetailView: View {
    @ObservedObject var store: BikeStore
    @ObservedObject private var updater = UpdaterController.shared
    @State private var launchAtLogin = LoginItem.isEnabled
    @State private var scheduleTime = Calendar.current.date(
        bySettingHour: 2, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var scheduleDaily = false
    @State private var showSchedule = false
    @State private var scheduleEvent = "charge.requested"
    @State private var shareMetrics = Analytics.isEnabled

    private var useImperial: Bool { store.useImperial }
    private func dist(_ km: Double) -> String {
        useImperial ? String(format: "%.1f mi", km*0.621371) : String(format: "%.1f km", km)
    }
    private func rangeVal(_ km: Double) -> Int { Int((useImperial ? km*0.621371 : km).rounded()) }
    private var distUnit: String { useImperial ? "mi" : "km" }

    /// System green is too light to read on the popover's grey material in light mode.
    /// Darken it there, brighten it in dark mode.
    private static let accentGreen = Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            ? NSColor(srgbRed: 0.35, green: 0.85, blue: 0.45, alpha: 1)
            : NSColor(srgbRed: 0.08, green: 0.44, blue: 0.18, alpha: 1)
    })

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(store.bikeName).font(.headline)

            if !store.serverUp {
                Label(store.lastError ?? "Server unreachable", systemImage: "wifi.slash")
                    .font(.caption).foregroundStyle(.secondary)
            } else if !store.loggedIn {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Not logged in", systemImage: "person.crop.circle.badge.questionmark")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Connect your Bosch eBike Flow account to see battery, rides and range.")
                        .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            } else if let b = store.battery {
                HStack(spacing: 10) {
                    Image(systemName: (b.is_charging ?? false) ? "battery.100.bolt" : "battery.100")
                        .font(.title2).foregroundStyle(Self.accentGreen)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(b.level_percent ?? 0)%").font(.title2).bold()
                        Text(statusLine(b)).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if (b.is_charging ?? false), let eta = store.chargeEta {
                            let tilde = store.chargeEtaEstimated ? "~" : ""
                            let note = store.chargeEtaEstimated ? " (est.)" : ""
                            Text("\(tilde)\(fmtCountdown(eta.timeIntervalSinceNow)) to full\(note)")
                                .font(.caption).fontWeight(.medium)
                                .foregroundStyle(Self.accentGreen)
                                .padding(.horizontal, 5).padding(.vertical, 2)
                                .background(Self.accentGreen.opacity(0.14), in: Capsule())
                        }
                    }
                }
                if let ranges = b.range_per_mode, !ranges.isEmpty {
                    HStack(spacing: 8) {
                        ForEach(ranges.indices, id: \.self) { i in
                            VStack(spacing: 1) {
                                Text("\(rangeVal(ranges[i].range_km ?? 0))").font(.callout).bold()
                                Text(ranges[i].mode ?? "").font(.system(size: 9)).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 4)
                            .background(Color.secondary.opacity(0.1))
                            .cornerRadius(6)
                        }
                    }
                }
                if let odo = b.odometer_km {
                    Label("\(dist(odo)) odometer", systemImage: "gauge.with.dots.needle.bottom.50percent")
                        .font(.caption)
                }
            }

            if let latest = store.rides.first {
                Divider()
                Text("Latest ride").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Text(shortDate(latest.start)).font(.caption)
                    Spacer()
                    Text(dist(latest.distance_km ?? 0)).font(.caption).bold()
                    if latest.has_gps ?? false { Image(systemName: "location.fill").font(.system(size: 9)).foregroundStyle(.green) }
                }
            }

            Divider()

            // Account / token
            HStack(spacing: 8) {
                if store.loggedIn {
                    Image(systemName: "checkmark.seal.fill").foregroundStyle(Self.accentGreen)
                    Text(store.loginUser.map { "Signed in · \($0.prefix(8))…" } ?? "Signed in")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                } else {
                    Image(systemName: "key.fill").foregroundStyle(.orange)
                    Text("Sign in to Bosch").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let s = store.loginStatus {
                Text(s).font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button(store.loggedIn ? "Re-authenticate…" : "Sign in…") {
                    Task { await store.beginLogin() }
                }
                // paste fallback only appears while a sign-in is underway (auto-capture is the norm)
                if store.loginInProgress {
                    Button("Paste code…") { promptForCode(store) }
                }
            }.font(.caption)

            HStack {
                Toggle("Launch at login", isOn: $launchAtLogin)
                    .toggleStyle(.checkbox).font(.caption)
                    .onChange(of: launchAtLogin) { on in LoginItem.set(on) }
                Spacer()
                Button("Check for Updates…") { UpdaterController.shared.checkForUpdates() }
                    .font(.caption).disabled(!updater.canCheck)
            }

            Toggle("Share anonymous usage stats", isOn: $shareMetrics)
                .toggleStyle(.checkbox).font(.caption)
                .onChange(of: shareMetrics) { on in Analytics.setEnabled(on) }
                .help("Sends an anonymous \"app is running\" ping (app version + macOS version only). No account, bike, or location data is ever sent.")

            if store.chargeTriggerAvailable {
                Divider()
                    .onAppear {
                        if !store.canStart { scheduleEvent = "charge.stopped" }
                    }
                // Labels stay terse: the panel is a fixed 300pt and "Begin charging"
                // plus "Stop charging" plus "Schedule…" overflows it. The row label
                // carries the meaning instead.
                // Two rows: four controls plus a label will not fit the 300pt panel.
                if store.canStart {
                    HStack {
                        Text("Charge to").foregroundColor(.secondary)
                        Spacer()
                        Button("80%") {
                            Task { await store.trigger("charge.requested", target: 80) }
                        }
                        Button("100%") {
                            Task { await store.trigger("charge.requested", target: 100) }
                        }
                    }.font(.caption)
                }
                HStack {
                    Text(store.chargeTarget == 100 ? "Full charge"
                                                   : "Stops near \(store.chargeTarget)%")
                        .foregroundColor(.secondary)
                    Spacer()
                    if store.canStop {
                        Button("Stop") {
                            Task { await store.trigger("charge.stopped") }
                        }
                    }
                    Button(showSchedule ? "Cancel" : "Schedule…") {
                        withAnimation { showSchedule.toggle() }
                    }
                }.font(.caption)

                if showSchedule {
                    VStack(alignment: .leading, spacing: 6) {
                        Picker("", selection: $scheduleEvent) {
                            if store.canStart { Text("Begin").tag("charge.requested") }
                            if store.canStop { Text("Stop").tag("charge.stopped") }
                        }
                        .pickerStyle(.segmented).labelsHidden()
                        HStack(spacing: 6) {
                            DatePicker("", selection: $scheduleTime,
                                       displayedComponents: .hourAndMinute)
                                .labelsHidden().datePickerStyle(.field)
                                .fixedSize()
                            Toggle("Daily", isOn: $scheduleDaily)
                                .toggleStyle(.checkbox)
                            Spacer(minLength: 0)
                            Button("Set") {
                                Task {
                                    await store.scheduleCharging(event: scheduleEvent,
                                                                 at: scheduleTime,
                                                                 daily: scheduleDaily)
                                    withAnimation { showSchedule = false }
                                }
                            }
                        }
                    }.font(.caption)
                }

                ForEach(store.chargeSchedules) { sched in
                    HStack {
                        Text("\(sched.event == "charge.stopped" ? "Stop" : "Begin") at \(sched.at)"
                             + (sched.repeat_ == "daily" ? " daily" : ""))
                            .font(.caption).foregroundColor(.secondary)
                        Spacer()
                        Button("Remove") { Task { await store.deleteSchedule(sched.id) } }
                            .font(.caption)
                    }
                }

                if let status = store.triggerStatus {
                    Text(status).font(.caption).foregroundColor(.secondary)
                }
            }

            Divider()
            HStack {
                Button("Dashboard") { NSWorkspace.shared.open(URL(string: apiBase)!) }
                Button("Refresh") { Task { await store.refresh() } }
                Button(distUnit) { Task { await store.setImperial(!store.useImperial) } }
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }.font(.caption)
        }
        .padding(14)
        .frame(width: 300)
    }

    private func statusLine(_ b: Battery) -> String {
        chargingStatus(connected: b.charger_connected, charging: b.is_charging,
                       level: b.level_percent)
    }
    private func shortDate(_ iso: String?) -> String {
        guard let iso, let d = ISO8601DateFormatter().date(from: iso) else { return "—" }
        let f = DateFormatter(); f.dateFormat = "MMM d, h:mm a"; return f.string(from: d)
    }
}

/// Paste fallback for the login flow: prompt for the `code` (or full redirect
/// URL) when the onebikeapp-ios:// auto-capture didn't fire.
@MainActor
func promptForCode(_ store: BikeStore) {
    let alert = NSAlert()
    alert.messageText = "Paste login code"
    alert.informativeText = "After logging in, copy the code from the onebikeapp-ios:// redirect (or paste the whole URL) here."
    alert.addButton(withTitle: "Save")
    alert.addButton(withTitle: "Cancel")
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
    field.placeholderString = "code or onebikeapp-ios://…"
    alert.accessoryView = field
    NSApp.activate(ignoringOtherApps: true)
    if alert.runModal() == .alertFirstButtonReturn {
        Task { await store.submitPastedCode(field.stringValue) }
    }
}

// MARK: - Status bar
@MainActor
final class StatusBarController: NSObject {
    let store = BikeStore()
    private var item: NSStatusItem!
    private let popover = NSPopover()
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()

    override init() {
        super.init()
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.action = #selector(togglePopover(_:))
            button.target = self
        }
        popover.behavior = .transient
        popover.animates = true

        store.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak self] in DispatchQueue.main.async { self?.updateButton() } }
            .store(in: &cancellables)

        updateButton()
        Notifier.requestAuth()
        Task { await store.refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.store.refresh() }
        }
        // ticks the charge countdown between polls
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateButton() }
        }
    }

    private func updateButton() {
        guard let button = item.button else { return }
        let title = store.menuTitle
        let attr = NSMutableAttributedString()
        if let icon = NSImage(systemSymbolName: "bicycle", accessibilityDescription: "bike") {
            let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
            let att = NSTextAttachment(); att.image = icon.withSymbolConfiguration(cfg) ?? icon
            attr.append(NSAttributedString(attachment: att))
            attr.append(NSAttributedString(string: " "))
        }
        attr.append(NSAttributedString(string: title, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)]))
        button.attributedTitle = attr
    }

    @objc private func togglePopover(_ sender: NSStatusBarButton) {
        if popover.isShown { popover.performClose(nil); return }
        Task { await store.loadUnits() }   // reflect a km/mi change made on the web
        let host = NSHostingController(rootView: DetailView(store: store))
        host.sizingOptions = .preferredContentSize   // lets the popover anchor to the button
        popover.contentViewController = host
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }
}

// MARK: - Analytics (anonymous usage — Aptabase)
// One privacy-preserving signal so we can see how many installs are active. No
// account, bike, location, or personal data is ever sent — only the app version,
// macOS version, and a rotating session id. Aptabase itself stores no IP address
// or device identifier (https://aptabase.com/privacy). Users can turn this off in
// Settings → "Share anonymous usage stats".
enum Analytics {
    // Public ingest key (safe to embed — every Aptabase-instrumented app ships one).
    private static let appKey = "A-US-7742302879"
    private static let defaultsKey = "metricsEnabled"

    private static var sessionId = UUID().uuidString
    private static var lastTouch = Date.distantPast
    private static var heartbeat: Timer?

    /// Opt-out defaults to ON: a missing key means enabled.
    static var isEnabled: Bool {
        UserDefaults.standard.object(forKey: defaultsKey) == nil
            ? true : UserDefaults.standard.bool(forKey: defaultsKey)
    }

    // Region is the middle segment of the key (A-US-…, A-EU-…, A-DEV-…).
    private static var ingestURL: URL? {
        let parts = appKey.split(separator: "-")
        guard parts.count >= 2 else { return nil }
        let host: String
        switch parts[1] {
        case "US":  host = "https://us.aptabase.com"
        case "EU":  host = "https://eu.aptabase.com"
        case "DEV": host = "http://localhost:3000"
        default:    return nil
        }
        return URL(string: "\(host)/api/v0/event")
    }

    /// Send `app_started` now, then a heartbeat twice a day so a machine left
    /// running still counts as active each day it is on.
    static func start() {
        guard isEnabled else { return }
        track("app_started")
        heartbeat?.invalidate()
        heartbeat = Timer.scheduledTimer(withTimeInterval: 12 * 3600, repeats: true) { _ in
            track("heartbeat")
        }
    }

    static func setEnabled(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: defaultsKey)
        if on { start() } else { heartbeat?.invalidate(); heartbeat = nil }
    }

    static func track(_ event: String) {
        guard isEnabled, let url = ingestURL else { return }
        // Aptabase groups events into sessions; roll a new id after an hour idle.
        if Date().timeIntervalSince(lastTouch) > 3600 { sessionId = UUID().uuidString }
        lastTouch = Date()

        let info = Bundle.main.infoDictionary
        let os = ProcessInfo.processInfo.operatingSystemVersion
        #if DEBUG
        let isDebug = true
        #else
        let isDebug = false
        #endif
        let body: [String: Any] = [
            "timestamp": iso8601.string(from: Date()),
            "sessionId": sessionId,
            "eventName": event,
            "systemProps": [
                "isDebug": isDebug,
                "osName": "macOS",
                "osVersion": "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)",
                "locale": Locale.current.identifier.replacingOccurrences(of: "_", with: "-"),
                "appVersion": info?["CFBundleShortVersionString"] as? String ?? "?",
                "appBuildNumber": info?["CFBundleVersion"] as? String ?? "?",
                "sdkVersion": "bikebar-swift@1.0",
            ],
        ]
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(appKey, forHTTPHeaderField: "App-Key")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()   // fire-and-forget; ignore failures
    }

    private static let iso8601: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()
}

// MARK: - App
final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: StatusBarController?
    let backend = BackendController()

    func applicationDidFinishLaunching(_ n: Notification) {
        controller = StatusBarController()      // shows the menu bar item immediately
        _ = UpdaterController.shared             // start Sparkle (background update checks)
        claimURLScheme()                         // own onebikeapp-ios:// so login redirects land here
        backend.start()                          // no-op in dev; spawns the bundled server otherwise
        Analytics.start()                        // anonymous "app is running" ping (opt-out in Settings)
        Task { [weak self] in
            await self?.backend.waitUntilHealthy()
            await self?.controller?.store.refresh()
        }
    }

    /// Register as the default handler for the Bosch login scheme, so the browser
    /// hands the onebikeapp-ios:// redirect straight to us. Self-heals wherever the
    /// app lives; only claims if we're not already the resolved handler.
    private func claimURLScheme() {
        let scheme = "onebikeapp-ios"
        let ws = NSWorkspace.shared
        let me = Bundle.main.bundleURL
        if let cur = URL(string: "\(scheme)://x"),
           ws.urlForApplication(toOpen: cur)?.standardizedFileURL == me.standardizedFileURL {
            return  // already ours
        }
        ws.setDefaultApplication(at: me, toOpenURLsWithScheme: scheme) { err in
            if let err { NSLog("could not claim \(scheme): \(err)") }
        }
    }

    /// macOS routes the onebikeapp-ios:// login redirect here (registered scheme).
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let store = controller?.store else { return }
        for url in urls where url.scheme == "onebikeapp-ios" {
            Task { await store.handleRedirect(url) }
        }
    }

    func applicationWillTerminate(_ n: Notification) {
        backend.stop()
    }
}

// Bootstrap (top-level code, so no @main). .accessory = menu bar only, no Dock.
let app = NSApplication.shared
let appDelegate = AppDelegate()
app.delegate = appDelegate
app.setActivationPolicy(.accessory)
app.run()
