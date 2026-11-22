import CryptoKit
import Foundation

@main
struct SignUpdateRollout {
    static func main() {
        do {
            try sign(Array(CommandLine.arguments.dropFirst()))
        } catch {
            FileHandle.standardError.write(Data("Could not sign the rollout feed. \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    static func sign(_ args: [String]) throws {
        guard args.count == 6, ["stable", "dev"].contains(args[3]),
              let percentage = UInt8(args[4]), percentage <= 100, let required = Bool(args[5]) else {
            throw failure("Provide LEGACY_FEED OUTPUT PRIVATE_SEED stable|dev PERCENTAGE true|false.")
        }
        let feed = try Data(contentsOf: URL(fileURLWithPath: args[0]))
        let destination = URL(fileURLWithPath: args[1]).standardizedFileURL
        guard destination != URL(fileURLWithPath: args[0]).standardizedFileURL,
              destination != URL(fileURLWithPath: args[2]).standardizedFileURL,
              (1...UpdateRollout.legacyLimit).contains(feed.count) else {
            throw failure("Use a separate output file and a valid legacy feed.")
        }
        let seed = try Data(contentsOf: URL(fileURLWithPath: args[2]))
        let key = try Curve25519.Signing.PrivateKey(rawRepresentation: seed)
        let legacy = try JSONDecoder().decode(SignedUpdateEnvelope.self, from: feed)
        guard key.publicKey.isValidSignature(legacy.signature, for: legacy.payload) else {
            throw failure("The legacy feed must be signed by the same key.")
        }

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let policy = UpdateRollout(schema: UpdateRollout.schemaVersion, channel: args[3],
                                   percentage: percentage, required: required, feed: feed)
        let payload = try encoder.encode(policy)
        let envelope = SignedUpdateEnvelope(payload: payload, signature: try key.signature(for: payload))
        let output = try encoder.encode(envelope)
        guard output.count <= UpdateRollout.envelopeLimit else { throw failure("The rollout feed is too large.") }
        try output.write(to: destination, options: .atomic)
    }

    static func failure(_ message: String) -> NSError {
        NSError(domain: "JaroReleaseSigning", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
