const std = @import("std");
const builtin = @import("builtin");
const native_sdk = @import("native_sdk");
const runner = @import("runner");

pub const panic = std.debug.FullPanic(native_sdk.debug.capturePanic);

const activation_timer: u64 = 1;
const startup_id_limit = 512;
const first_window: u64 = 1;
const GtkWindow = opaque {};
const GtkApplication = opaque {};
const WindowList = extern struct { data: *GtkWindow, next: ?*WindowList, previous: ?*WindowList };
extern fn g_application_get_default() ?*GtkApplication;
extern fn gtk_application_get_windows(*GtkApplication) ?*const WindowList;
extern fn gtk_window_set_startup_id(*GtkWindow, [*:0]const u8) void;

const win = std.os.windows;
extern "kernel32" fn GetStdHandle(win.DWORD) callconv(.winapi) ?win.HANDLE;
extern "kernel32" fn PeekNamedPipe(win.HANDLE, ?*anyopaque, win.DWORD, ?*win.DWORD, ?*win.DWORD, ?*win.DWORD) callconv(.winapi) win.BOOL;
extern "kernel32" fn ReadFile(win.HANDLE, [*]u8, win.DWORD, *win.DWORD, ?*anyopaque) callconv(.winapi) win.BOOL;

fn pendingInput(buffer: []u8) ?usize {
    if (builtin.os.tag == .windows) {
        const input = GetStdHandle(@bitCast(@as(i32, -10))) orelse return null; // STD_INPUT_HANDLE
        var available: win.DWORD = 0;
        if (!PeekNamedPipe(input, null, 0, null, &available, null).toBool()) return null;
        if (available == 0) return 0;
        var count: win.DWORD = 0;
        if (!ReadFile(input, buffer.ptr, @intCast(@min(available, buffer.len)), &count, null).toBool()) return null;
        return count;
    }
    const posix = std.posix;
    var descriptors = [_]posix.pollfd{.{ .fd = posix.STDIN_FILENO, .events = posix.POLL.IN, .revents = 0 }};
    if ((posix.poll(&descriptors, 0) catch return null) == 0) return 0;
    if (descriptors[0].revents & posix.POLL.IN == 0) return null;
    const count = posix.read(posix.STDIN_FILENO, buffer) catch return null;
    return if (count == 0) null else count;
}

const Activation = struct {
    token: [startup_id_limit:0]u8 = @splat(0),
    used: usize = 0,

    fn start(_: *anyopaque, runtime: *native_sdk.Runtime) anyerror!void {
        try runtime.startTimer(activation_timer, 150 * std.time.ns_per_ms, true);
    }

    fn event(context: *anyopaque, runtime: *native_sdk.Runtime, received: native_sdk.Event) anyerror!void {
        if (received != .timer or received.timer.id != activation_timer) return;
        var bytes: [startup_id_limit + 1]u8 = undefined;
        const count = pendingInput(&bytes) orelse {
            try runtime.cancelTimer(activation_timer);
            return;
        };
        const self: *Activation = @ptrCast(@alignCast(context));
        for (bytes[0..count]) |byte| {
            if (byte == '\n') {
                if (builtin.os.tag == .linux and self.used > 0) {
                    if (g_application_get_default()) |app| {
                        if (gtk_application_get_windows(app)) |windows| {
                            self.token[self.used] = 0;
                            gtk_window_set_startup_id(windows.data, &self.token);
                        }
                    }
                }
                self.used = 0;
                runtime.showWindow(first_window) catch |err| {
                    std.log.warn("Could not show the running Jaro window: {s}", .{@errorName(err)});
                };
            } else if (self.used < startup_id_limit) {
                self.token[self.used] = byte;
                self.used += 1;
            }
        }
    }
};

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

    var context: Activation = .{};
    try runner.runWithOptions(.{
        .context = &context,
        .name = "jaro",
        .source = native_sdk.WebViewSource.url(page),
        .start_fn = Activation.start,
        .event_fn = Activation.event,
    }, .{
        .app_name = "Jaro",
        .window_title = "Jaro",
        .bundle_id = "local.jardaudio.desktop",
        .security = .{ .navigation = .{ .allowed_origins = &.{origin} } },
    }, init);
}
