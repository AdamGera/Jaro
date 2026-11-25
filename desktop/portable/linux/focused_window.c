/* Short-lived read-only snapshot of the compositor's activated toplevel. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>
#include "wlr-foreign-toplevel-management-unstable-v1.h"

typedef struct zwlr_foreign_toplevel_handle_v1 Handle;
typedef struct zwlr_foreign_toplevel_manager_v1 Manager;

struct Window {
    struct wl_list link;
    char *application;
    char *title;
    bool active;
    bool complete;
};

struct Snapshot {
    Manager *manager;
    struct wl_list windows;
};

static void replace(char **field, const char *value, size_t limit) {
    free(*field);
    *field = NULL;
    if (strlen(value) <= limit && !(*field = strdup(value))) exit(1);
}

static void title(void *data, Handle *handle, const char *value) {
    (void)handle;
    struct Window *window = data;
    replace(&window->title, value, 4096);
    window->complete = false;
}

static void application(void *data, Handle *handle, const char *value) {
    (void)handle;
    struct Window *window = data;
    replace(&window->application, value, 256);
    window->complete = false;
}

static void output(void *data, Handle *handle, struct wl_output *display) {
    (void)data; (void)handle; (void)display;
}

static void state(void *data, Handle *handle, struct wl_array *states) {
    (void)handle;
    struct Window *window = data;
    window->active = false;
    window->complete = false;
    uint32_t *item;
    wl_array_for_each(item, states) {
        if (*item == ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_ACTIVATED) window->active = true;
    }
}

static void done(void *data, Handle *handle) {
    (void)handle;
    ((struct Window *)data)->complete = true;
}

static void closed(void *data, Handle *handle) {
    struct Window *window = data;
    wl_list_remove(&window->link);
    free(window->application);
    free(window->title);
    free(window);
    zwlr_foreign_toplevel_handle_v1_destroy(handle);
}

/* Bind version 1. The parent event was added in version 3. */
static const struct zwlr_foreign_toplevel_handle_v1_listener events = {
    .title = title, .app_id = application, .output_enter = output, .output_leave = output,
    .state = state, .done = done, .closed = closed,
};

static void toplevel(void *data, Manager *manager, Handle *handle) {
    (void)manager;
    struct Snapshot *snapshot = data;
    struct Window *window = calloc(1, sizeof(*window));
    if (!window) exit(1);
    wl_list_insert(&snapshot->windows, &window->link);
    zwlr_foreign_toplevel_handle_v1_add_listener(handle, &events, window);
}

static void finished(void *data, Manager *manager) {
    (void)manager;
    ((struct Snapshot *)data)->manager = NULL;
}

static const struct zwlr_foreign_toplevel_manager_v1_listener manager_events = {toplevel, finished};

static void global(void *data, struct wl_registry *registry, uint32_t name, const char *iface, uint32_t version) {
    struct Snapshot *snapshot = data;
    const struct wl_interface *interface = &zwlr_foreign_toplevel_manager_v1_interface;
    if (version && !snapshot->manager && !strcmp(iface, interface->name)) {
        snapshot->manager = wl_registry_bind(registry, name, interface, 1);
        if (!snapshot->manager) exit(1);
        zwlr_foreign_toplevel_manager_v1_add_listener(snapshot->manager, &manager_events, snapshot);
    }
}

static void removed(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data; (void)registry; (void)name;
}

int main(void) {
    struct Snapshot snapshot = {0};
    wl_list_init(&snapshot.windows);
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) return 1;
    struct wl_registry *registry = wl_display_get_registry(display);
    static const struct wl_registry_listener globals = {global, removed};
    if (!registry || wl_registry_add_listener(registry, &globals, &snapshot) < 0) return 1;
    /* Discover the manager, then receive its initial window events. */
    if (wl_display_roundtrip(display) < 0 || !snapshot.manager ||
        wl_display_roundtrip(display) < 0 || !snapshot.manager) return 1;

    struct Window *window, *focused = NULL;
    wl_list_for_each(window, &snapshot.windows, link) {
        if (!window->complete) return 1;
        if (window->active) {
            if (focused) return 1;
            focused = window;
        }
    }
    if (!focused || !focused->application || !*focused->application) return 1;
    const char *label = focused->title ? focused->title : "";
    wl_list_for_each(window, &snapshot.windows, link) {
        if (window != focused && window->application && window->title &&
            !strcmp(window->application, focused->application) && !strcmp(window->title, label)) {
            label = "";
            break;
        }
    }
    /* Wayland strings cannot contain NUL. Titles can contain newlines. */
    printf("%s%c%s", focused->application, 0, label);
    wl_display_disconnect(display);
    return fflush(stdout) == EOF;
}
