#pragma once

#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include <atomic>

inline BOOL WINAPI wambridge_probe_write_file(
    HANDLE file,
    LPCVOID buffer,
    DWORD bytesToWrite,
    LPDWORD bytesWritten,
    LPOVERLAPPED overlapped
) {
    static std::atomic<unsigned> calls{0};
    const unsigned call = calls.fetch_add(1);
    const ULONGLONG started = GetTickCount64();
    const BOOL result = ::WriteFile(
        file,
        buffer,
        bytesToWrite,
        bytesWritten,
        overlapped
    );
    if (call < 8) {
        const DWORD error = result ? ERROR_SUCCESS : GetLastError();
        // console::printf is pfc's formatter, not the CRT one: it prints the
        // length modifiers in %lu and %llu literally and drops the value.
        // Measured on 2026-08-02, which is why this probe reported
        // "requested=lu written=lu elapsedMs=llu" and diagnosed nothing.
        console::printf(
            "WAM Bridge Output: PCM WriteFile #%u result=%u requested=%u "
            "written=%u elapsedMs=%u error=%u",
            call + 1,
            result ? 1U : 0U,
            static_cast<unsigned>(bytesToWrite),
            static_cast<unsigned>(bytesWritten == nullptr ? 0 : *bytesWritten),
            static_cast<unsigned>(GetTickCount64() - started),
            static_cast<unsigned>(error)
        );
    }
    return result;
}

#define WriteFile wambridge_probe_write_file
