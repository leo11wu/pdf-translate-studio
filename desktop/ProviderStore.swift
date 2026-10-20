import Foundation
import Security

enum StudioError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let value) = self { return value }; return nil }
}

struct RuntimePreferences: Codable, Equatable {
    var concurrency_mode = "auto"
    var max_in_flight = 16
    var request_rate = 2.0
    var batch_tokens = 1536
    var reasoning_policy = "lowest"

    init() {}
    init(values: [String: Any]) {
        concurrency_mode = values["concurrency_mode"] as? String == "manual" ? "manual" : "auto"
        max_in_flight = max(1, min(values["max_in_flight"] as? Int ?? 16, concurrency_mode == "auto" ? 16 : 64))
        request_rate = max(0.1, min(values["request_rate"] as? Double ?? 2, 64))
        batch_tokens = max(128, min(values["batch_tokens"] as? Int ?? 1536, 8192))
        reasoning_policy = values["reasoning_policy"] as? String == "provider_default" ? "provider_default" : "lowest"
    }
    var values: [String: Any] {
        ["concurrency_mode": concurrency_mode, "max_in_flight": max_in_flight,
         "request_rate": request_rate, "batch_tokens": batch_tokens, "reasoning_policy": reasoning_policy]
    }
}

struct ProviderProfile: Codable {
    var id = ""
    var name = ""
    var base_url = "https://api.openai.com/v1"
    var model = ""
    var api_key = ""
    var qps = 8
    var runtime = RuntimePreferences()
    init() {}
    enum CodingKeys: String, CodingKey { case id, name, base_url, model, api_key, qps, runtime }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        base_url = try c.decodeIfPresent(String.self, forKey: .base_url) ?? "https://api.openai.com/v1"
        model = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        api_key = try c.decodeIfPresent(String.self, forKey: .api_key) ?? ""
        qps = try c.decodeIfPresent(Int.self, forKey: .qps) ?? 8
        runtime = try c.decodeIfPresent(RuntimePreferences.self, forKey: .runtime) ?? RuntimePreferences()
    }
    func publicValues(remembered: Bool) -> [String: Any] {
        ["id": id, "name": name, "base_url": base_url, "model": model, "qps": qps,
         "runtime": runtime.values, "key_available": !api_key.isEmpty, "remembered": remembered]
    }
}

protocol SecretStore {
    func read() throws -> Data?
    func save(_ data: Data) throws
    func delete() throws
}

struct KeychainStore: SecretStore {
    let service: String
    var account = "current-provider"
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: account, kSecAttrSynchronizable as String: false]
    }
    func read() throws -> Data? {
        var search = query
        search[kSecReturnData as String] = true
        search[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(search as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw StudioError.message("无法读取钥匙串（\(status)）。请解锁登录钥匙串后重试。") }
        return result as? Data
    }
    func save(_ data: Data) throws {
        let updated = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw StudioError.message("无法更新钥匙串（\(updated)）。") }
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrLabel as String] = "纸间 · API 配置"
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw StudioError.message("无法保存到钥匙串（\(status)），原配置仍保留。") }
    }
    func delete() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw StudioError.message("无法删除钥匙串记录（\(status)）。") }
    }
}

// The persisted schema deliberately cannot encode a credential.
struct ProviderRecord: Codable {
    var id: String
    var name: String
    var base_url: String
    var model: String
    var qps: Int
    var runtime: RuntimePreferences
    var remembered: Bool
    var last_used: Double
    init(_ p: ProviderProfile, remembered: Bool) {
        id = p.id; name = p.name; base_url = p.base_url; model = p.model; qps = p.qps
        runtime = p.runtime; self.remembered = remembered; last_used = Date().timeIntervalSince1970
    }
    func profile() -> ProviderProfile {
        var p = ProviderProfile()
        p.id = id; p.name = name; p.base_url = base_url; p.model = model; p.qps = qps; p.runtime = runtime
        return p
    }
}

struct ProviderVault: Codable {
    var schema = 2
    var active_profile_id = ""
    var profiles: [ProviderRecord] = []
}

final class ProviderStore {
    var session = ProviderProfile()
    private var vault = ProviderVault()
    private var loaded = false
    private let readPreferences: () -> Data?
    private let writePreferences: (Data) throws -> Void
    private let legacyMetadata: () -> [String: Any]?
    private let secrets: (String) -> SecretStore
    private let send: (ProviderProfile) throws -> Void

    init(read: @escaping () -> Data?, write: @escaping (Data) throws -> Void,
         legacy: @escaping () -> [String: Any]?, secrets: @escaping (String) -> SecretStore,
         send: @escaping (ProviderProfile) throws -> Void) {
        readPreferences = read; writePreferences = write; legacyMetadata = legacy
        self.secrets = secrets; self.send = send
    }

    private func persist(_ value: ProviderVault) throws {
        try writePreferences(JSONEncoder().encode(value))
        vault = value
    }

    func load() throws {
        if loaded { return }
        if let data = readPreferences() {
            guard let saved = try? JSONDecoder().decode(ProviderVault.self, from: data), saved.schema == 2 else {
                throw StudioError.message("API 历史记录无法读取，未覆盖现有记录。")
            }
            vault = saved
        } else if let old = legacyMetadata() {
            var p = ProviderProfile()
            p.id = UUID().uuidString
            p.base_url = old["base_url"] as? String ?? p.base_url
            p.model = old["model"] as? String ?? ""
            p.qps = old["qps"] as? Int ?? 8
            p.runtime.max_in_flight = p.qps
            p.name = URL(string: p.base_url)?.host ?? "原有配置"
            if let data = try secrets("current-provider").read(),
               let saved = try? JSONDecoder().decode(ProviderProfile.self, from: data), saved.base_url == p.base_url {
                p.api_key = saved.api_key
            }
            let remembered = !p.api_key.isEmpty
            if remembered { try secrets(p.id).save(Data(p.api_key.utf8)) }
            var migrated = ProviderVault()
            migrated.active_profile_id = p.id
            migrated.profiles = [ProviderRecord(p, remembered: remembered)]
            do { try persist(migrated) }
            catch { if remembered { try? secrets(p.id).delete() }; throw error }
            // Keep the legacy entry for rollback; forget/delete also removes it.
        }
        if !vault.active_profile_id.isEmpty { try select(vault.active_profile_id) }
        else { try send(session) }
        loaded = true
    }

    func select(_ id: String) throws {
        if id.isEmpty {
            let blank = ProviderProfile()
            try send(blank)
            session = blank
            return
        }
        guard let record = vault.profiles.first(where: { $0.id == id }) else { throw StudioError.message("找不到这条 API 配置。") }
        var p = record.profile()
        if record.remembered, let data = try secrets(id).read() { p.api_key = String(data: data, encoding: .utf8) ?? "" }
        try send(p)
        var updated = vault
        updated.active_profile_id = id
        if let index = updated.profiles.firstIndex(where: { $0.id == id }) { updated.profiles[index].last_used = Date().timeIntervalSince1970 }
        do { try persist(updated) }
        catch { try? send(session); throw error }
        session = p
    }

    func save(_ proposed: ProviderProfile, remember: Bool) throws {
        var p = proposed
        if p.api_key.isEmpty && p.id == session.id && p.base_url == session.base_url { p.api_key = session.api_key }
        guard !p.api_key.isEmpty, !p.model.isEmpty else { throw StudioError.message("请完整填写 API 地址、密钥和模型。") }
        let create = p.id.isEmpty || p.id != session.id || p.base_url != session.base_url || p.api_key != session.api_key
        if create { p.id = UUID().uuidString }
        if p.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let host = URL(string: p.base_url)?.host ?? "自定义 API"
            let count = vault.profiles.filter { $0.base_url == p.base_url }.count
            p.name = host + (count > 0 && create ? " (\(count + 1))" : "")
        } else if create && p.name == session.name && p.base_url == session.base_url {
            let count = vault.profiles.filter { $0.base_url == p.base_url }.count
            p.name += " (\(count + 1))"
        }
        if remember { try secrets(p.id).save(Data(p.api_key.utf8)) }
        do {
            try send(p)
            var updated = vault
            updated.profiles.removeAll { $0.id == p.id }
            if remember { updated.profiles.append(ProviderRecord(p, remembered: true)); updated.active_profile_id = p.id }
            else { updated.active_profile_id = "" }
            try persist(updated)
        } catch {
            if create && remember { try? secrets(p.id).delete() }
            try? send(session)
            throw error
        }
        if !remember { try secrets(p.id).delete() }
        session = p
    }

    func forget(deleteRecord: Bool = false) throws {
        let id = session.id
        guard !id.isEmpty else { return }
        let oldSecret = try secrets(id).read()
        let oldLegacy = try secrets("current-provider").read()
        var cleared = session
        cleared.api_key = ""
        do {
            try send(cleared)
            try secrets(id).delete()
            // Remove the legacy entry only for its matching migrated profile.
            if let data = oldLegacy,
               let old = try? JSONDecoder().decode(ProviderProfile.self, from: data), old.base_url == session.base_url,
               old.api_key == session.api_key { try secrets("current-provider").delete() }
            var updated = vault
            if deleteRecord { updated.profiles.removeAll { $0.id == id }; updated.active_profile_id = ""; cleared = ProviderProfile(); try send(cleared) }
            else if let index = updated.profiles.firstIndex(where: { $0.id == id }) { updated.profiles[index].remembered = false }
            try persist(updated)
        } catch {
            if let data = oldSecret { try? secrets(id).save(data) }
            if let data = oldLegacy { try? secrets("current-provider").save(data) }
            try? send(session)
            throw error
        }
        session = cleared
    }

    func publicValues() -> [String: Any] {
        let remembered = vault.profiles.first(where: { $0.id == session.id })?.remembered ?? false
        var result = session.publicValues(remembered: remembered)
        result["active_profile_id"] = session.id
        result["profiles"] = vault.profiles.sorted { $0.last_used > $1.last_used }.map { record -> [String: Any] in
            var p = record.profile().publicValues(remembered: record.remembered)
            p["last_used"] = record.last_used
            return p
        }
        return result
    }
}
