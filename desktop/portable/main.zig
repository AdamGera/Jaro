const std = @import("std");
const native_sdk = @import("native_sdk");
const runner = @import("runner");

pub const panic = std.debug.FullPanic(native_sdk.debug.capturePanic);

pub fn main(init: std.process.Init) !void {
    const environment = init.environ_map;
    const port = try std.fmt.parseInt(u16, environment.get("JARO_WORKSPACE_PORT") orelse
        return error.MissingWorkspacePort, 10);
    if (port == 0) return error.InvalidWorkspacePort;
    const key = environment.get("JARO_WORKSPACE_KEY") orelse return error.MissingWorkspaceKey;
    if (key.len != 64) return error.InvalidWorkspaceKey;
    for (key) |byte| {
        if (!(std.ascii.isDigit(byte) or (byte >= 'a' and byte <= 'f'))) return error.InvalidWorkspaceKey;
    }

    var origin_buffer: [32]u8 = undefined;
    const origin = try std.fmt.bufPrint(&origin_buffer, "http://127.0.0.1:{d}", .{port});
    var page_buffer: [128]u8 = undefined;
    const page = try std.fmt.bufPrint(&page_buffer, "{s}/_desktop/connect#{s}", .{ origin, key });
    _ = environment.orderedRemove("JARO_WORKSPACE_KEY");

    var context: u8 = 0;
    try runner.runWithOptions(.{
        .context = &context,
        .name = "jaro",
        .source = native_sdk.WebViewSource.url(page),
    }, .{
        .app_name = "Jaro",
        .window_title = "Jaro",
        .bundle_id = "local.jardaudio.desktop",
        .security = .{ .navigation = .{ .allowed_origins = &.{origin} } },
    }, init);
}
