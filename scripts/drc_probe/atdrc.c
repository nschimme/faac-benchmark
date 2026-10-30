/* atdrc: decode AAC (ADTS or MP4) through Apple AudioToolbox with the
 * decoder-side DRC property set. Writes raw interleaved float32.
 *
 * usage: atdrc in.aac|in.m4a out.f32 drc_mode(0=none,1=light,2=heavy) [target(0..3)]
 *
 * Apple exposes the settings on the AudioConverter inside ExtAudioFile. The
 * kAudioConverterPropertySettings reset is deliberately not used; it faults on
 * this decoder. afconvert and FFmpeg's aac_at never set this property, so they
 * cannot be used for DRC application checks.
 */
#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    if (argc < 4 || argc > 5) {
        fprintf(stderr, "usage: %s in.aac|in.m4a out.f32 drc_mode(0..2) [target(0..3)]\n", argv[0]);
        return 2;
    }

    CFURLRef url = CFURLCreateFromFileSystemRepresentation(
        NULL, (const UInt8 *)argv[1], (CFIndex)strlen(argv[1]), false);
    if (!url)
        return 2;

    ExtAudioFileRef file = NULL;
    OSStatus status = ExtAudioFileOpenURL(url, &file);
    CFRelease(url);
    if (status != noErr) {
        fprintf(stderr, "open %s: %d\n", argv[1], (int)status);
        return 1;
    }

    AudioStreamBasicDescription file_format;
    UInt32 size = sizeof(file_format);
    status = ExtAudioFileGetProperty(file, kExtAudioFileProperty_FileDataFormat, &size, &file_format);
    if (status != noErr || file_format.mChannelsPerFrame == 0) {
        fprintf(stderr, "file format: %d\n", (int)status);
        ExtAudioFileDispose(file);
        return 1;
    }

    AudioStreamBasicDescription client = { 0 };
    client.mSampleRate = file_format.mSampleRate;
    client.mFormatID = kAudioFormatLinearPCM;
    client.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked;
    client.mChannelsPerFrame = file_format.mChannelsPerFrame;
    client.mBitsPerChannel = 32;
    client.mBytesPerFrame = client.mChannelsPerFrame * sizeof(float);
    client.mFramesPerPacket = 1;
    client.mBytesPerPacket = client.mBytesPerFrame;
    status = ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat, sizeof(client), &client);
    if (status != noErr) {
        fprintf(stderr, "client format: %d\n", (int)status);
        ExtAudioFileDispose(file);
        return 1;
    }

    AudioConverterRef converter = NULL;
    size = sizeof(converter);
    status = ExtAudioFileGetProperty(file, kExtAudioFileProperty_AudioConverter, &size, &converter);
    if (status != noErr) {
        fprintf(stderr, "converter: %d\n", (int)status);
        ExtAudioFileDispose(file);
        return 1;
    }

    UInt32 mode = (UInt32)atoi(argv[3]);
    status = AudioConverterSetProperty(converter, kAudioCodecPropertyDynamicRangeControlMode,
                                       sizeof(mode), &mode);
    fprintf(stderr, "drc mode %u: %d\n", (unsigned)mode, (int)status);
    if (status != noErr) {
        ExtAudioFileDispose(file);
        return 1;
    }
    if (argc == 5) {
        UInt32 target = (UInt32)atoi(argv[4]);
        status = AudioConverterSetProperty(converter, kAudioCodecPropertyProgramTargetLevelConstant,
                                           sizeof(target), &target);
        fprintf(stderr, "target %u: %d\n", (unsigned)target, (int)status);
        if (status != noErr) {
            ExtAudioFileDispose(file);
            return 1;
        }
    }

    FILE *out = fopen(argv[2], "wb");
    if (!out) {
        ExtAudioFileDispose(file);
        return 1;
    }

    enum { FRAMES = 4096 };
    float *samples = malloc(FRAMES * client.mBytesPerFrame);
    if (!samples) {
        fclose(out);
        ExtAudioFileDispose(file);
        return 1;
    }
    uint64_t total = 0;
    for (;;) {
        AudioBufferList buffers;
        buffers.mNumberBuffers = 1;
        buffers.mBuffers[0].mNumberChannels = client.mChannelsPerFrame;
        buffers.mBuffers[0].mDataByteSize = FRAMES * client.mBytesPerFrame;
        buffers.mBuffers[0].mData = samples;
        UInt32 frames = FRAMES;
        status = ExtAudioFileRead(file, &frames, &buffers);
        if (status != noErr) {
            fprintf(stderr, "read: %d\n", (int)status);
            break;
        }
        if (frames == 0)
            break;
        fwrite(samples, client.mBytesPerFrame, frames, out);
        total += frames;
    }

    fprintf(stderr, "sr=%.0f ch=%u frames=%llu\n", client.mSampleRate,
            (unsigned)client.mChannelsPerFrame, (unsigned long long)total);
    free(samples);
    fclose(out);
    ExtAudioFileDispose(file);
    return status == noErr ? 0 : 1;
}
