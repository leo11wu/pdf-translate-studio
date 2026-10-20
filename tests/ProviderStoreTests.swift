import Foundation

final class MemoryState {
    var preferences: Data?
    var keys: [String: Data] = [:]
    var sent: [ProviderProfile] = []
    var failWrite = false
    var failSend = false
}
struct MemorySecret: SecretStore {
    var state: MemoryState
    var id: String
    func read() throws -> Data? { state.keys[id] }
    func save(_ data: Data) throws { state.keys[id] = data }
    func delete() throws { state.keys.removeValue(forKey: id) }
}
enum TestError: Error { case failed(String) }
func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw TestError.failed(message) }
}

@main struct ProviderStoreTests {
    static func main() throws {
        let state = MemoryState()
        var old = ProviderProfile()
        old.base_url = "https://one.test/v1"; old.model = "model-a"; old.api_key = "fixture-key-A"
        state.keys["current-provider"] = try JSONEncoder().encode(old)
        let legacy: [String: Any] = ["base_url": old.base_url, "model": old.model, "qps": 4]
        func store() -> ProviderStore {
            ProviderStore(read: { state.preferences }, write: { data in
                if state.failWrite { throw TestError.failed("injected storage failure") }
                state.preferences = data
            }, legacy: { legacy }, secrets: { MemorySecret(state: state, id: $0) }, send: { p in
                if state.failSend { throw TestError.failed("injected worker failure") }
                state.sent.append(p)
            })
        }
        let first = store()
        try first.load()
        let idA = first.session.id
        try require(first.session.api_key == "fixture-key-A", "migration lost key")
        try require(first.session.runtime.reasoning_policy == "lowest", "new default not applied")
        try require(!String(data: state.preferences!, encoding: .utf8)!.contains("fixture-key"), "credential leaked to preferences")
        var other = ProviderProfile()
        other.base_url = "https://two.test/custom/v1"; other.model = "model-b"; other.api_key = "fixture-key-B"; other.name = "Second API"
        try first.save(other, remember: true)
        let idB = first.session.id
        try first.select(idA)
        try require(first.session.api_key == "fixture-key-A" && first.session.base_url == old.base_url, "cross-provider key mismatch")
        var rotated = first.session
        rotated.api_key = "fixture-key-C"
        try first.save(rotated, remember: true)
        let idC = first.session.id
        try require(idC != idA && idC != idB, "rotation overwrote history")
        try first.select(idB)
        var edited = first.session
        edited.model = "model-b2"; edited.name = "Renamed"; edited.runtime.max_in_flight = 7
        try first.save(edited, remember: true)
        try require(first.session.id == idB, "ordinary edit created duplicate")
        let restarted = store()
        try restarted.load()
        try require(restarted.session.api_key == "fixture-key-B" && restarted.session.model == "model-b2", "restart did not restore selected profile")
        let publicData = try JSONSerialization.data(withJSONObject: restarted.publicValues())
        try require(!String(data: publicData, encoding: .utf8)!.contains("fixture-key"), "credential leaked through native bridge")
        try restarted.forget(deleteRecord: true)
        try require(state.keys[idB] == nil && state.keys[idA] != nil && state.keys[idC] != nil, "delete affected another profile")
        try restarted.select(idA)
        try restarted.forget()
        try require(state.keys[idA] == nil && state.keys["current-provider"] == nil && state.keys[idC] != nil, "forget isolation or legacy cleanup failed")
        try restarted.select(idC)
        let originalPreferences = state.preferences
        state.failSend = true
        var failed = restarted.session
        failed.base_url = "https://failed.test/v1"; failed.api_key = "fixture-key-failure"
        do { try restarted.save(failed, remember: true); throw TestError.failed("failure was swallowed") }
        catch TestError.failed(let value) where value == "injected worker failure" {}
        try require(state.preferences == originalPreferences && restarted.session.id == idC, "failed save changed active record")
        try require(!state.keys.values.contains(Data("fixture-key-failure".utf8)), "failed save left orphan secret")
        state.failSend = false
        state.failWrite = true
        do { try restarted.select(idA); throw TestError.failed("storage failure was swallowed") }
        catch TestError.failed(let value) where value == "injected storage failure" {}
        try require(state.sent.last?.id == idC && restarted.session.id == idC, "failed select left backend on wrong provider")
        do { try restarted.forget(deleteRecord: true); throw TestError.failed("delete storage failure was swallowed") }
        catch TestError.failed(let value) where value == "injected storage failure" {}
        try require(state.keys[idC] == Data("fixture-key-C".utf8), "failed deletion lost selected credential")
        let migration = MemoryState()
        migration.keys["current-provider"] = try JSONEncoder().encode(old)
        let retryMigration = ProviderStore(read: { nil }, write: { _ in throw TestError.failed("injected storage failure") },
            legacy: { legacy }, secrets: { MemorySecret(state: migration, id: $0) }, send: { _ in })
        do { try retryMigration.load(); throw TestError.failed("migration storage failure was swallowed") }
        catch TestError.failed(let value) where value == "injected storage failure" {}
        try require(migration.keys.count == 1 && migration.keys["current-provider"] != nil, "failed migration removed original or left an orphan")
        print("Provider history: migration, rotation, switching, restart, isolation, rollback and redaction PASS")
    }
}
