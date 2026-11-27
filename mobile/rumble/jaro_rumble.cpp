#include "jaro_rumble.h"

#include "core/bpe.h"
#include "core/qwen3_prompt.h"
#include "qwen3_asr.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <sys/stat.h>
#include <thread>

struct jaro_rumble_model {
    std::mutex inference;
    std::unique_ptr<qwen3_asr_context, decltype(&qwen3_asr_free)> context{nullptr, qwen3_asr_free};
};

namespace {

constexpr off_t modelSize = 444897792;
constexpr int vocabularySize = 99630;
constexpr int minimumSamples = JARO_RUMBLE_SAMPLE_RATE / 10;
constexpr char transcriptMarker[] = "<asr_text>";
constexpr char languagePrefix[] = "language ";

std::mutex jobsMutex;
std::map<uint64_t, std::atomic<bool> *> activeJobs;
// An asynchronous caller can cancel between assigning an ID and entering the
// native call. Keep recent cancellations of IDs that haven't registered yet.
std::array<uint64_t, 64> earlyCancellations{};
size_t nextEarlyCancellation = 0;

class Job {
public:
    explicit Job(uint64_t id) : id(id) {
        std::lock_guard lock(jobsMutex);
        const auto early = std::find(earlyCancellations.begin(), earlyCancellations.end(), id);
        if (early != earlyCancellations.end()) {
            *early = 0;
            return;
        }
        registered = activeJobs.emplace(id, &cancelled).second;
    }

    ~Job() { finish(); }
    Job(const Job &) = delete;
    Job &operator=(const Job &) = delete;

    bool stopped() const { return !registered || cancelled.load(std::memory_order_relaxed); }

    bool finish() {
        if (!registered) return false;
        std::lock_guard lock(jobsMutex);
        activeJobs.erase(id);
        registered = false;
        return !cancelled.load(std::memory_order_relaxed);
    }

private:
    const uint64_t id;
    bool registered = false;
    std::atomic<bool> cancelled{false};
};

template <typename T>
using Buffer = std::unique_ptr<T, decltype(&std::free)>;

bool isModelFile(const char *path) {
    if (!path || path[0] != '/') return false;
    struct stat info{};
    if (stat(path, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size != modelSize) return false;
    std::unique_ptr<FILE, decltype(&std::fclose)> file(std::fopen(path, "rb"), std::fclose);
    char magic[4];
    return file && std::fread(magic, 1, sizeof(magic), file.get()) == sizeof(magic)
        && std::memcmp(magic, "GGUF", sizeof(magic)) == 0;
}

bool hasSignal(const float *pcm, int samples) {
    // Check short windows for quiet words in clips with long pauses. Removing
    // each window's DC offset avoids recognizing a constant input as speech.
    constexpr int window = JARO_RUMBLE_SAMPLE_RATE / 20;
    constexpr double minimumVariance = 0.000001; // -60 dBFS RMS
    bool signal = false;
    for (int start = 0; start < samples; start += window) {
        const int length = std::min(window, samples - start);
        double sum = 0;
        double squareSum = 0;
        for (int i = 0; i < length; ++i) {
            const double value = pcm[start + i];
            if (!std::isfinite(value) || std::abs(value) > 1) return false;
            sum += value;
            squareSum += value * value;
        }
        const double mean = sum / length;
        signal |= squareSum / length - mean * mean >= minimumVariance;
    }
    return signal;
}

bool validLanguage(const char *name) {
    if (!name) return true;
    const size_t length = strnlen(name, JARO_RUMBLE_LANGUAGE_CAPACITY);
    if (length == 0 || length == JARO_RUMBLE_LANGUAGE_CAPACITY || name[0] == ' ' || name[length - 1] == ' ') return false;
    return std::all_of(name, name + length, [](char c) {
        return ('A' <= c && c <= 'Z') || ('a' <= c && c <= 'z') || c == ' ' || c == '-';
    });
}

int tokenId(qwen3_asr_context *context, const char *token) {
    int count = 0;
    Buffer<int32_t> ids(qwen3_asr_tokenize(context, token, &count), std::free);
    return ids && count == 1 ? ids.get()[0] : -1;
}

int bestToken(qwen3_asr_context *context, const float *embedding, int count, int past) {
    int rows = 0;
    int columns = 0;
    Buffer<float> logits(qwen3_asr_run_llm_kv(context, embedding, count, past, &rows, &columns), std::free);
    if (!logits || rows < 1 || rows > count || columns != vocabularySize) return -1;
    const float *lastRow = logits.get() + static_cast<size_t>(rows - 1) * columns;
    return static_cast<int>(std::max_element(lastRow, lastRow + columns) - lastRow);
}

std::string decode(qwen3_asr_context *context, const float *pcm, int samples,
                   const char *language, const Job &job, std::string &recognizedLanguage) {
    int bins = 0;
    int frames = 0;
    Buffer<float> mel(qwen3_asr_compute_mel(context, pcm, samples, &bins, &frames), std::free);
    if (!mel || job.stopped()) return {};

    int audioTokens = 0;
    int width = 0;
    Buffer<float> audio(qwen3_asr_run_encoder(context, mel.get(), bins, frames, &audioTokens, &width), std::free);
    mel.reset();
    if (!audio || audioTokens < 1 || width != 1024 || job.stopped()) return {};

    const std::string prefill = language ? std::string(languagePrefix) + language + transcriptMarker : "";
    const auto prompt = core_qwen3_prompt::build(audioTokens, qwen3_asr_system_prefix(context), "", prefill, "");
    int count = 0;
    Buffer<int32_t> tokens(qwen3_asr_tokenize(context, prompt.c_str(), &count), std::free);
    if (!tokens || count < audioTokens) return {};
    Buffer<float> embeddings(qwen3_asr_embed_tokens(context, tokens.get(), count), std::free);
    const int audioPadding = tokenId(context, "<|audio_pad|>");
    const int end = tokenId(context, "<|im_end|>");
    const int endOfText = tokenId(context, "<|endoftext|>");
    if (!embeddings || audioPadding < 0 || end < 0 || endOfText < 0) return {};

    int spliced = 0;
    for (int i = 0; i < count; ++i) {
        if (tokens.get()[i] != audioPadding) continue;
        if (spliced == audioTokens) return {};
        std::memcpy(embeddings.get() + static_cast<size_t>(i) * width,
                    audio.get() + static_cast<size_t>(spliced) * width, width * sizeof(float));
        ++spliced;
    }
    audio.reset();
    const int maxGenerated = std::max(96, samples * 16 / JARO_RUMBLE_SAMPLE_RATE);
    constexpr int cacheSize = 512;
    if (spliced != audioTokens || count + maxGenerated > cacheSize || job.stopped()
        || !qwen3_asr_kv_init(context, cacheSize)) return {};

    qwen3_asr_kv_reset(context);
    int id = bestToken(context, embeddings.get(), count, 0);
    embeddings.reset();
    std::string output;
    for (int step = 0; step < maxGenerated; ++step) {
        if (id < 0 || job.stopped()) return {};
        if (id == end || id == endOfText) break;
        const char *token = qwen3_asr_token_text(context, id);
        if (token && std::strncmp(token, "<|", 2) != 0) output += core_bpe::token_bytes_to_utf8(token);
        if (step + 1 == maxGenerated) return {}; // Treat truncated output as a failed transcription.
        Buffer<float> next(qwen3_asr_embed_tokens(context, &id, 1), std::free);
        if (!next) return {};
        id = bestToken(context, next.get(), 1, count + step);
    }

    const auto marker = output.find(transcriptMarker);
    if (language) recognizedLanguage = language;
    if (marker != std::string::npos) {
        if (!language && output.rfind(languagePrefix, 0) == 0 && marker >= sizeof(languagePrefix) - 1) {
            recognizedLanguage = output.substr(sizeof(languagePrefix) - 1, marker - (sizeof(languagePrefix) - 1));
        }
        output.erase(0, marker + sizeof(transcriptMarker) - 1);
    } else if (!language) return {};
    // The model describes non-speech sounds between these tags. Skip them
    // while preserving any words the model recognized outside the span.
    for (auto tag = output.find("<non_speech>"); tag != std::string::npos; tag = output.find("<non_speech>", tag)) {
        const auto endTag = output.find("</non_speech>", tag);
        output.erase(tag, endTag == std::string::npos ? std::string::npos : endTag - tag + sizeof("</non_speech>") - 1);
    }
    const auto first = output.find_first_not_of(" \t\r\n");
    if (first == std::string::npos || output.find('\0') != std::string::npos) return {};
    return output.substr(first, output.find_last_not_of(" \t\r\n") - first + 1);
}

} // namespace

extern "C" jaro_rumble_model *jaro_rumble_open(const char *path, int threads) {
    try {
        if (!isModelFile(path)) return nullptr;
        // The tokenizer's shared lookup tables are lazily initialized upstream.
        static std::once_flag tokenizerSetup;
        std::call_once(tokenizerSetup, [] { (void)core_bpe::byte_decoder(); });
        auto model = std::make_unique<jaro_rumble_model>();
        auto params = qwen3_asr_context_default_params();
        const auto cores = std::thread::hardware_concurrency();
        const auto requested = threads > 0 ? static_cast<unsigned>(threads) : 4u;
        params.n_threads = std::min(requested, cores > 0 ? std::min(cores, 8u) : 8u);
        params.use_gpu = false;
        params.verbosity = 0;
        model->context.reset(qwen3_asr_init_from_file(path, params));
        if (!model->context || qwen3_asr_lm_head_dim(model->context.get()) != vocabularySize) return nullptr;
        return model.release();
    } catch (...) {
        return nullptr;
    }
}

extern "C" char *jaro_rumble_transcribe(jaro_rumble_model *model, const float *pcm,
                                        int samples, const char *language, uint64_t id) {
    return jaro_rumble_transcribe_detected(model, pcm, samples, language, id, nullptr);
}

extern "C" char *jaro_rumble_transcribe_detected(jaro_rumble_model *model, const float *pcm,
                                                 int samples, const char *language, uint64_t id,
                                                 char *detectedLanguage) {
    if (detectedLanguage) detectedLanguage[0] = '\0';
    if (id == 0) return nullptr;
    try {
        Job job(id);
        if (job.stopped() || !model || !pcm || samples < minimumSamples || samples > JARO_RUMBLE_MAX_SAMPLES
            || !validLanguage(language) || !hasSignal(pcm, samples)) return nullptr;
        std::lock_guard lock(model->inference);
        if (job.stopped()) return nullptr;
        std::string name;
        const auto result = decode(model->context.get(), pcm, samples, language, job, name);
        if (!job.finish() || result.empty()) return nullptr;
        char *text = strdup(result.c_str());
        // Model metadata may use Unicode, for example Norwegian Bokmål. The ASCII
        // check for prompt input applies only to a caller's requested language.
        if (text && detectedLanguage && name.size() < JARO_RUMBLE_LANGUAGE_CAPACITY
            && name.find('\0') == std::string::npos) {
            std::memcpy(detectedLanguage, name.c_str(), name.size() + 1);
        }
        return text;
    } catch (...) {
        return nullptr;
    }
}

extern "C" void jaro_rumble_cancel(uint64_t id) {
    if (id == 0) return;
    std::lock_guard lock(jobsMutex);
    if (const auto active = activeJobs.find(id); active != activeJobs.end()) {
        active->second->store(true, std::memory_order_relaxed);
        return;
    }
    earlyCancellations[nextEarlyCancellation] = id;
    nextEarlyCancellation = (nextEarlyCancellation + 1) % earlyCancellations.size();
}

extern "C" void jaro_rumble_free_text(char *text) { std::free(text); }

extern "C" void jaro_rumble_close(jaro_rumble_model *model) { delete model; }
