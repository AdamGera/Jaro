#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#include "ext-data-control-v1.h"
#include "wlr-data-control-unstable-v1.h"

enum { MAX_FORMATS = 128, MAX_MIME = 4096, MAX_BYTES = 32 * 1024 * 1024 };
enum { DEVICE_DATA_OFFER, DEVICE_SELECTION, DEVICE_FINISHED, DEVICE_PRIMARY_SELECTION };
enum { SOURCE_SEND, SOURCE_CANCELLED };

struct Format {
    const char *mime;
    unsigned char *data;
    size_t length;
};

struct Offer {
    struct wl_proxy *proxy;
    struct Offer *next;
    char *mimes[MAX_FORMATS];
    size_t count;
    bool incomplete;
};

struct Clipboard {
    struct wl_display *display;
    uint32_t ext_manager, wlr_manager, seat;
    bool virtual_keyboard;
    bool reported, finished;
    unsigned revision;
    const struct wl_interface *source_interface;
    struct wl_proxy *manager, *device, *source;
    struct Offer *selection, *primary, *pending;
    const struct Format *served;
    size_t served_count;
};

/* Both version-1 protocols use identical clipboard requests and events. */
_Static_assert(EXT_DATA_CONTROL_MANAGER_V1_CREATE_DATA_SOURCE == ZWLR_DATA_CONTROL_MANAGER_V1_CREATE_DATA_SOURCE, "create");
_Static_assert(EXT_DATA_CONTROL_MANAGER_V1_GET_DATA_DEVICE == ZWLR_DATA_CONTROL_MANAGER_V1_GET_DATA_DEVICE, "device");
_Static_assert(EXT_DATA_CONTROL_DEVICE_V1_SET_SELECTION == ZWLR_DATA_CONTROL_DEVICE_V1_SET_SELECTION, "selection");
_Static_assert(EXT_DATA_CONTROL_SOURCE_V1_OFFER == ZWLR_DATA_CONTROL_SOURCE_V1_OFFER, "offer");
_Static_assert(EXT_DATA_CONTROL_SOURCE_V1_DESTROY == ZWLR_DATA_CONTROL_SOURCE_V1_DESTROY, "source destroy");
_Static_assert(EXT_DATA_CONTROL_OFFER_V1_RECEIVE == ZWLR_DATA_CONTROL_OFFER_V1_RECEIVE, "receive");
_Static_assert(EXT_DATA_CONTROL_OFFER_V1_DESTROY == ZWLR_DATA_CONTROL_OFFER_V1_DESTROY, "offer destroy");

static int64_t now_ms(void) {
    struct timespec time;
    clock_gettime(CLOCK_MONOTONIC, &time);
    return time.tv_sec * INT64_C(1000) + time.tv_nsec / 1000000;
}

static bool fd_wait(int fd, short events, int64_t deadline) {
    struct pollfd descriptor = {.fd = fd, .events = events};
    int result;
    do {
        int64_t remaining = deadline - now_ms();
        if (remaining <= 0) return false;
        result = poll(&descriptor, 1, (int)remaining);
    } while (result < 0 && errno == EINTR);
    return result > 0 && (descriptor.revents & (events | POLLHUP));
}

static bool flush(struct Clipboard *clipboard, int64_t deadline) {
    while (wl_display_flush(clipboard->display) < 0) {
        if (errno != EAGAIN || !fd_wait(wl_display_get_fd(clipboard->display), POLLOUT, deadline))
            return false;
    }
    return true;
}

static void drop_offer(struct Offer *offer) {
    if (!offer) return;
    for (size_t i = 0; i < offer->count; ++i) free(offer->mimes[i]);
    wl_proxy_marshal_flags(offer->proxy, EXT_DATA_CONTROL_OFFER_V1_DESTROY, NULL, 1, WL_MARSHAL_FLAG_DESTROY);
    free(offer);
}

static int offered_mime(const void *implementation, void *data, uint32_t opcode,
                        const struct wl_message *message, union wl_argument *args) {
    (void)implementation;
    (void)opcode;
    (void)message;
    /* This dispatcher's target is the proxy; its user data is our offer. */
    struct Offer *offer = wl_proxy_get_user_data(data);
    const char *mime = args[0].s;
    if (!mime || !*mime || strlen(mime) > MAX_MIME) {
        offer->incomplete = true;
        return 0;
    }
    for (size_t i = 0; i < offer->count; ++i)
        if (!strcmp(offer->mimes[i], mime)) return 0;
    if (offer->count == MAX_FORMATS || !(offer->mimes[offer->count] = strdup(mime))) {
        offer->incomplete = true;
        return 0;
    }
    ++offer->count;
    return 0;
}

static int device_event(const void *implementation, void *data, uint32_t opcode,
                        const struct wl_message *message, union wl_argument *args) {
    (void)implementation;
    (void)message;
    struct Clipboard *clipboard = wl_proxy_get_user_data(data);
    if (opcode == DEVICE_DATA_OFFER) {
        struct Offer *offer = calloc(1, sizeof(*offer));
        if (!offer) {
            clipboard->finished = true;
            return 0;
        }
        offer->proxy = (struct wl_proxy *)args[0].o;
        offer->next = clipboard->pending;
        clipboard->pending = offer;
        if (wl_proxy_add_dispatcher(offer->proxy, offered_mime, NULL, offer) < 0)
            clipboard->finished = true;
    } else if (opcode == DEVICE_SELECTION || opcode == DEVICE_PRIMARY_SELECTION) {
        struct Offer *next = NULL;
        if (args[0].o) {
            struct Offer **pending = &clipboard->pending;
            while (*pending && (*pending)->proxy != (struct wl_proxy *)args[0].o)
                pending = &(*pending)->next;
            if (!*pending) {
                clipboard->finished = true;
                return 0;
            }
            next = *pending;
            *pending = next->next;
            next->next = NULL;
        }
        struct Offer **slot = opcode == DEVICE_SELECTION ? &clipboard->selection : &clipboard->primary;
        drop_offer(*slot);
        *slot = next;
        if (opcode == DEVICE_SELECTION) {
            clipboard->reported = true;
            ++clipboard->revision;
        }
    } else if (opcode == DEVICE_FINISHED) {
        clipboard->finished = true;
    }
    return 0;
}

static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)registry;
    (void)version;
    struct Clipboard *clipboard = data;
    if (!strcmp(interface, ext_data_control_manager_v1_interface.name)) clipboard->ext_manager = name;
    if (!strcmp(interface, zwlr_data_control_manager_v1_interface.name)) clipboard->wlr_manager = name;
    if (!strcmp(interface, wl_seat_interface.name) && !clipboard->seat) clipboard->seat = name;
    if (!strcmp(interface, "zwp_virtual_keyboard_manager_v1")) clipboard->virtual_keyboard = true;
}

static void global_removed(void *data, struct wl_registry *registry, uint32_t name) {
    (void)registry;
    struct Clipboard *clipboard = data;
    if (clipboard->seat == name) clipboard->finished = true;
}

static const struct wl_registry_listener registry_listener = {global, global_removed};

static struct wl_registry *connect_seat(struct Clipboard *clipboard) {
    clipboard->display = wl_display_connect(NULL);
    if (!clipboard->display) return NULL;
    struct wl_registry *registry = wl_display_get_registry(clipboard->display);
    if (!registry || wl_registry_add_listener(registry, &registry_listener, clipboard) < 0 ||
        wl_display_roundtrip(clipboard->display) < 0 || !clipboard->seat) return NULL;
    return registry;
}

static bool open_clipboard(struct Clipboard *clipboard) {
    struct wl_registry *registry = connect_seat(clipboard);
    if (!registry) return false;
    bool standard = clipboard->ext_manager != 0;
    uint32_t manager = standard ? clipboard->ext_manager : clipboard->wlr_manager;
    if (!manager) return false;
    const struct wl_interface *manager_interface = standard ? &ext_data_control_manager_v1_interface
                                                           : &zwlr_data_control_manager_v1_interface;
    const struct wl_interface *device_interface = standard ? &ext_data_control_device_v1_interface
                                                          : &zwlr_data_control_device_v1_interface;
    clipboard->source_interface = standard ? &ext_data_control_source_v1_interface
                                           : &zwlr_data_control_source_v1_interface;
    struct wl_seat *seat = wl_registry_bind(registry, clipboard->seat, &wl_seat_interface, 1);
    clipboard->manager = wl_registry_bind(registry, manager, manager_interface, 1);
    if (!seat || !clipboard->manager) return false;
    clipboard->device = wl_proxy_marshal_flags(clipboard->manager, EXT_DATA_CONTROL_MANAGER_V1_GET_DATA_DEVICE,
                                               device_interface, 1, 0, NULL, seat);
    return clipboard->device && wl_proxy_add_dispatcher(clipboard->device, device_event, NULL, clipboard) == 0 &&
           wl_display_roundtrip(clipboard->display) >= 0 && clipboard->reported && !clipboard->finished;
}

struct KeyboardMap {
    bool has_keyboard;
    struct xkb_keymap *keymap;
};

static void seat_capabilities(void *data, struct wl_seat *seat, uint32_t caps) {
    (void)seat;
    struct KeyboardMap *keyboard = data;
    keyboard->has_keyboard = (caps & WL_SEAT_CAPABILITY_KEYBOARD) != 0;
}

static int keyboard_event(const void *implementation, void *proxy, uint32_t opcode,
                          const struct wl_message *message, union wl_argument *args) {
    (void)implementation;
    (void)opcode;
    if (strcmp(message->name, "keymap")) return 0;
    struct KeyboardMap *keyboard = wl_proxy_get_user_data(proxy);
    xkb_keymap_unref(keyboard->keymap);
    keyboard->keymap = NULL;
    int fd = args[1].h;
    uint32_t size = args[2].u;
    if (args[0].u == WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 && size > 1 && size <= 4 * 1024 * 1024) {
        char *text = malloc(size);
        // The compositor may share the same fd offset with other clients.
        if (text && pread(fd, text, size, 0) == (ssize_t)size && text[size - 1] == '\0') {
            struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
            if (context) {
                keyboard->keymap = xkb_keymap_new_from_string(context, text, XKB_KEYMAP_FORMAT_TEXT_V1,
                                                             XKB_KEYMAP_COMPILE_NO_FLAGS);
                xkb_context_unref(context);
            }
        }
        free(text);
    }
    close(fd);
    return 0;
}

/* Return zero for a missing symbol and -1 if the layout groups disagree. */
static int common_keycode(struct xkb_keymap *keymap, xkb_keysym_t symbol,
                          xkb_layout_index_t first_group, xkb_layout_index_t end_group) {
    enum { EVDEV_OFFSET = 8 };
    xkb_keycode_t first = xkb_keymap_min_keycode(keymap);
    xkb_keycode_t last = xkb_keymap_max_keycode(keymap);
    if (first <= EVDEV_OFFSET) first = EVDEV_OFFSET + 1;
    if (last > UINT16_MAX) return 0;
    int agreed = 0;
    bool missing = false;
    for (xkb_layout_index_t group = first_group; group < end_group; ++group) {
        int match = 0;
        for (xkb_keycode_t code = first; code <= last; ++code) {
            const xkb_keysym_t *symbols;
            if (xkb_keymap_key_get_syms_by_level(keymap, code, group, 0, &symbols) == 1 && symbols[0] == symbol) {
                match = code - EVDEV_OFFSET;
                break;
            }
        }
        if (match) {
            if (agreed && match != agreed) return -1;
            agreed = match;
        } else {
            missing = true;
        }
    }
    return missing && agreed ? -1 : agreed;
}

static int paste_keycodes(struct Clipboard *clipboard, const char *group) {
    unsigned long requested_group = 0;
    if (group) {
        char *end;
        requested_group = strtoul(group, &end, 10);
        if (end == group || *end || requested_group > UINT16_MAX) return 2;
    }
    struct wl_registry *registry = connect_seat(clipboard);
    if (!registry) return 3;
    struct KeyboardMap layout = {0};
    const struct wl_seat_listener listener = {.capabilities = seat_capabilities};
    struct wl_seat *seat = wl_registry_bind(registry, clipboard->seat, &wl_seat_interface, 1);
    if (!seat || wl_seat_add_listener(seat, &listener, &layout) < 0 ||
        wl_display_roundtrip(clipboard->display) < 0 || !layout.has_keyboard) return 3;
    struct wl_keyboard *keyboard = wl_seat_get_keyboard(seat);
    int result = 3;
    if (keyboard && wl_proxy_add_dispatcher((struct wl_proxy *)keyboard, keyboard_event, NULL, &layout) == 0 &&
        wl_display_roundtrip(clipboard->display) >= 0 && !clipboard->finished && layout.has_keyboard && layout.keymap) {
        xkb_layout_index_t count = xkb_keymap_num_layouts(layout.keymap);
        if (requested_group < count) {
            xkb_layout_index_t first = (xkb_layout_index_t)requested_group;
            xkb_layout_index_t end = group ? first + 1 : count;
            printf("%d %d %d %d\n",
                   common_keycode(layout.keymap, XKB_KEY_Control_L, first, end),
                   common_keycode(layout.keymap, XKB_KEY_Shift_L, first, end),
                   common_keycode(layout.keymap, XKB_KEY_v, first, end),
                   common_keycode(layout.keymap, XKB_KEY_XF86Paste, first, end));
            result = 0;
        }
    }
    xkb_keymap_unref(layout.keymap);
    return result;
}

enum ReadFormat { FORMAT_OK, FORMAT_UNAVAILABLE, FORMAT_TOO_LARGE };

static enum ReadFormat read_format(struct Clipboard *clipboard, struct Offer *offer, struct Format *value,
                                   size_t remaining, int64_t deadline) {
    int pipe_fds[2];
    if (pipe2(pipe_fds, O_CLOEXEC) < 0) return FORMAT_UNAVAILABLE;
    wl_proxy_marshal_flags(offer->proxy, EXT_DATA_CONTROL_OFFER_V1_RECEIVE, NULL, 1, 0, value->mime, pipe_fds[1]);
    close(pipe_fds[1]);
    enum ReadFormat result = FORMAT_UNAVAILABLE;
    unsigned char block[65536];
    if (flush(clipboard, deadline)) {
        size_t capacity = 0;
        int64_t first_byte = now_ms() + 250;
        if (first_byte > deadline) first_byte = deadline;
        while (fd_wait(pipe_fds[0], POLLIN, value->length ? deadline : first_byte)) {
            ssize_t size = read(pipe_fds[0], block, sizeof(block));
            if (size < 0 && errno == EINTR) continue;
            if (size == 0) {
                result = FORMAT_OK;
                break;
            }
            if (size < 0) break;
            if ((size_t)size > remaining - value->length) {
                result = FORMAT_TOO_LARGE;
                break;
            }
            size_t needed = value->length + (size_t)size;
            if (needed > capacity) {
                capacity = needed > capacity * 2 ? needed : capacity * 2;
                if (capacity > remaining) capacity = remaining;
                unsigned char *buffer = realloc(value->data, capacity);
                if (!buffer) break;
                value->data = buffer;
            }
            memcpy(value->data + value->length, block, (size_t)size);
            value->length = needed;
        }
    }
    close(pipe_fds[0]);
    return result;
}

static bool x11_selection_metadata(const char *mime) {
    const char *targets[] = {"TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS", "DELETE",
                             "INSERT_SELECTION", "INSERT_PROPERTY", "INCR", "ATOM_PAIR"};
    for (size_t i = 0; i < sizeof(targets) / sizeof(targets[0]); ++i)
        if (!strcmp(mime, targets[i])) return true;
    return false;
}

static bool snapshot(struct Clipboard *clipboard, struct Format *formats, size_t *count) {
    if (wl_display_roundtrip(clipboard->display) < 0 || clipboard->finished) return false;
    struct Offer *offer = clipboard->selection;
    if (!offer) return true;
    if (offer->incomplete || !offer->count) return false;
    unsigned revision = clipboard->revision;
    int64_t deadline = now_ms() + 1500;
    size_t remaining = MAX_BYTES;
    // Read the formats most apps can use before trying less common XWayland conversions.
    for (int pass = 0; pass < 2; ++pass) {
        for (size_t i = 0; i < offer->count; ++i) {
            const char *mime = offer->mimes[i];
            bool preferred = !strncmp(mime, "text/plain", 10) || !strcmp(mime, "UTF8_STRING") || !strcmp(mime, "image/png");
            if (preferred != (pass == 0) || x11_selection_metadata(mime)) continue;
            if (now_ms() >= deadline) break;
            struct Format entry = {.mime = strdup(mime)};
            if (!entry.mime) return false;
            enum ReadFormat result = read_format(clipboard, offer, &entry, remaining, deadline);
            if (result == FORMAT_OK) {
                formats[(*count)++] = entry;
                remaining -= entry.length;
            } else {
                free((void *)entry.mime);
                free(entry.data);
                if (result == FORMAT_TOO_LARGE) return false;
            }
        }
    }
    return *count && wl_display_roundtrip(clipboard->display) >= 0 && !clipboard->finished &&
           clipboard->revision == revision;
}

static void write_format(int fd, const struct Format *entry) {
    if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0) return;
    size_t offset = 0;
    int64_t deadline = now_ms() + 1000;
    while (offset < entry->length && fd_wait(fd, POLLOUT, deadline)) {
        ssize_t written = write(fd, entry->data + offset, entry->length - offset);
        if (written < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (written <= 0) break;
        offset += (size_t)written;
    }
}

static int source_event(const void *implementation, void *data, uint32_t opcode,
                        const struct wl_message *message, union wl_argument *args) {
    (void)implementation;
    (void)message;
    struct Clipboard *clipboard = wl_proxy_get_user_data(data);
    if (opcode == SOURCE_SEND) {
        int fd = args[1].h;
        if (clipboard->source == data) {
            for (size_t i = 0; i < clipboard->served_count; ++i) {
                const struct Format *format = clipboard->served + i;
                if (!strcmp(args[0].s, format->mime)) {
                    write_format(fd, format);
                    break;
                }
            }
        }
        close(fd);
    } else if (opcode == SOURCE_CANCELLED) {
        if (clipboard->source == data) clipboard->source = NULL;
        wl_proxy_marshal_flags(data, EXT_DATA_CONTROL_SOURCE_V1_DESTROY, NULL, 1, WL_MARSHAL_FLAG_DESTROY);
    }
    return 0;
}

static bool publish(struct Clipboard *clipboard, const struct Format *formats, size_t count) {
    struct wl_proxy *source = NULL;
    if (count) {
        source = wl_proxy_marshal_flags(clipboard->manager, EXT_DATA_CONTROL_MANAGER_V1_CREATE_DATA_SOURCE,
                                       clipboard->source_interface, 1, 0, NULL);
        if (!source || wl_proxy_add_dispatcher(source, source_event, NULL, clipboard) < 0) return false;
        for (size_t i = 0; i < count; ++i)
            wl_proxy_marshal_flags(source, EXT_DATA_CONTROL_SOURCE_V1_OFFER, NULL, 1, 0, formats[i].mime);
    }
    clipboard->source = source;
    clipboard->served = formats;
    clipboard->served_count = count;
    wl_proxy_marshal_flags(clipboard->device, EXT_DATA_CONTROL_DEVICE_V1_SET_SELECTION, NULL, 1, 0, source);
    return wl_display_roundtrip(clipboard->display) >= 0 && !clipboard->finished &&
           (!count || clipboard->source == source);
}

static void emit(const char *line) {
    puts(line);
    fflush(stdout);
}

/* Wait for another transcript or EOF while still serving the selection. */
static bool wait_for_command(struct Clipboard *clipboard) {
    struct pollfd descriptors[] = {
        {.fd = wl_display_get_fd(clipboard->display), .events = POLLIN},
        {.fd = STDIN_FILENO, .events = POLLIN},
    };
    while (clipboard->source && !clipboard->finished) {
        if (wl_display_dispatch_pending(clipboard->display) < 0 || !flush(clipboard, now_ms() + 1000))
            return false;
        if (!clipboard->source || clipboard->finished) break;
        int ready = poll(descriptors, 2, -1);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0 || descriptors[0].revents & (POLLERR | POLLHUP | POLLNVAL)) return false;
        if ((descriptors[0].revents & POLLIN) && wl_display_dispatch(clipboard->display) < 0) return false;
        if (descriptors[1].revents) return true;
    }
    return false;
}

static void keep_selection(struct Clipboard *clipboard) {
    // Like a clipboard owner started by the desktop, keep serving until another copy.
    pid_t child = fork();
    if (child < 0) _exit(1);
    if (child > 0) _exit(0);
    setsid();
    close(STDIN_FILENO);
    close(STDOUT_FILENO);
    close(STDERR_FILENO);
    while (clipboard->source && !clipboard->finished && wl_display_dispatch(clipboard->display) >= 0) {}
    _exit(0);
}

static unsigned char *read_transcript(size_t *length) {
    char header[32], *end;
    if (!fgets(header, sizeof(header), stdin)) return NULL;
    errno = 0;
    unsigned long parsed = strtoul(header, &end, 10);
    if (errno || end == header || strcmp(end, "\n") || !parsed || parsed > MAX_BYTES) return NULL;
    unsigned char *text = malloc(parsed);
    if (!text || fread(text, 1, parsed, stdin) != parsed) {
        free(text);
        return NULL;
    }
    *length = parsed;
    return text;
}

static int paste(struct Clipboard *clipboard) {
    size_t length;
    unsigned char *text = read_transcript(&length);
    if (!text) return 2;
    struct Format original[MAX_FORMATS] = {0};
    size_t count = 0;
    struct Format transcript[] = {
        {"text/plain;charset=utf-8", text, length}, {"text/plain", text, length}, {"UTF8_STRING", text, length},
    };
    int result = 3;
    if (snapshot(clipboard, original, &count) && publish(clipboard, transcript, 3)) {
        result = 0;
        bool restored = false;
        for (;;) {
            emit("ready");
            if (!wait_for_command(clipboard)) break;
            unsigned char *next = read_transcript(&length);
            // Reading a command can race another owner's copy.
            if (wl_display_roundtrip(clipboard->display) < 0 || !clipboard->source || clipboard->finished) {
                free(next);
                break;
            }
            if (!next) {
                if (publish(clipboard, original, count)) {
                    emit("restored");
                    if (count) keep_selection(clipboard);
                    restored = true;
                }
                break;
            }
            free(text);
            text = next;
            for (size_t i = 0; i < 3; ++i) {
                transcript[i].data = text;
                transcript[i].length = length;
            }
            if (!publish(clipboard, transcript, 3)) break;
        }
        if (!restored) emit("changed");
    }
    for (size_t i = 0; i < count; ++i) {
        free((void *)original[i].mime);
        free(original[i].data);
    }
    free(text);
    return result;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) return 2;
    bool keycodes = !strcmp(argv[1], "keycodes");
    if (!keycodes && (argc != 2 || (strcmp(argv[1], "probe") && strcmp(argv[1], "paste")))) return 2;
    signal(SIGPIPE, SIG_IGN);
    struct Clipboard clipboard = {0};
    int result = 3;
    if (keycodes) {
        result = paste_keycodes(&clipboard, argc == 3 ? argv[2] : NULL);
    } else if (open_clipboard(&clipboard)) {
        if (!strcmp(argv[1], "probe")) {
            emit(clipboard.virtual_keyboard ? "virtual" : "clipboard");
            result = 0;
        } else {
            result = paste(&clipboard);
        }
    }
    if (clipboard.display) {
        while (clipboard.pending) {
            struct Offer *offer = clipboard.pending;
            clipboard.pending = offer->next;
            drop_offer(offer);
        }
        drop_offer(clipboard.selection);
        drop_offer(clipboard.primary);
        wl_display_disconnect(clipboard.display);
    }
    return result;
}
