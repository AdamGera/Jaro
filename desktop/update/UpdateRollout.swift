import CryptoKit
import Foundation

struct SignedUpdateEnvelope: Codable {
    let payload: Data
    let signature: Data
}

struct UpdateRollout: Codable {
    static let schemaVersion = 2
    static let legacyLimit = 64 * 1024
    static let envelopeLimit = 128 * 1024

    let schema: Int
    let channel: String
    let percentage: UInt8
    let required: Bool
    let feed: Data

    enum CodingKeys: String, CodingKey {
        case schema, channel, required, feed
        case percentage = "rollout_percent"
    }
}

@_cdecl("JardVerifyRolloutFeed")
public func verifyRolloutFeed(
    _ bytes: UnsafeRawPointer,
    _ length: Int,
    _ publicKey: UnsafePointer<CChar>,
    _ channel: UnsafePointer<CChar>,
    _ legacy: UnsafeMutablePointer<UInt8>,
    _ capacity: Int,
    _ percentage: UnsafeMutablePointer<UInt8>,
    _ required: UnsafeMutablePointer<UInt8>
) -> Int {
    guard (1...UpdateRollout.envelopeLimit).contains(length),
          let rawKey = Data(base64Encoded: String(cString: publicKey)),
          let key = try? Curve25519.Signing.PublicKey(rawRepresentation: rawKey) else { return 0 }

    let decoder = JSONDecoder()
    guard let envelope = try? decoder.decode(SignedUpdateEnvelope.self, from: Data(bytes: bytes, count: length)),
          key.isValidSignature(envelope.signature, for: envelope.payload),
          let rollout = try? decoder.decode(UpdateRollout.self, from: envelope.payload),
          rollout.schema == UpdateRollout.schemaVersion,
          rollout.channel == String(cString: channel),
          rollout.percentage <= 100,
          !rollout.feed.isEmpty,
          rollout.feed.count <= min(capacity, UpdateRollout.legacyLimit) else { return 0 }

    rollout.feed.copyBytes(to: legacy, count: rollout.feed.count)
    percentage.pointee = rollout.percentage
    required.pointee = rollout.required ? 1 : 0
    return rollout.feed.count
}
