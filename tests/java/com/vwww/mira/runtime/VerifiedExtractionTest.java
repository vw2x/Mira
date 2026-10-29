package com.vwww.mira.runtime;

import java.io.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.*;

public final class VerifiedExtractionTest {
    private static final byte[] DATA = "verified runtime payload".getBytes();
    private static long crc(byte[] data) {
        CRC32 value = new CRC32(); value.update(data); return value.getValue();
    }
    private static void check(boolean value) {
        if (!value) throw new AssertionError();
    }
    private static void fails(Work work) throws Exception {
        try { work.run(); } catch (IOException expected) { return; }
        throw new AssertionError("Expected IOException");
    }
    interface Work { void run() throws Exception; }
    public static void main(String[] args) throws Exception {
        File root = new File(args[0]); root.mkdirs();
        if (args.length == 2) {
            int count = 0;
            try (ZipFile apk = new ZipFile(args[1])) {
                java.util.Enumeration<? extends ZipEntry> entries = apk.entries();
                String prefix = "assets/bootstrap/prefix/arm64-v8a/";
                while (entries.hasMoreElements()) {
                    ZipEntry entry = entries.nextElement();
                    if (entry.isDirectory() || !entry.getName().startsWith(prefix)) continue;
                    File target = new File(root, entry.getName().substring(prefix.length()));
                    check(target.getCanonicalPath().startsWith(root.getCanonicalPath() + File.separator));
                    VerifiedExtraction.extract(() -> apk.getInputStream(entry), target,
                        entry.getSize(), entry.getCrc(), false);
                    count++;
                }
            }
            check(count > 0);
            System.out.println("Verified APK entries: " + count);
            return;
        }
        File target = new File(root, "library.so");
        Files.write(target.toPath(), "old runtime".getBytes());
        AtomicInteger attempts = new AtomicInteger();
        VerifiedExtraction.extract(() -> {
            if (attempts.incrementAndGet() == 1) throw new IOException("transient read failure");
            return new ByteArrayInputStream(DATA);
        }, target, DATA.length, crc(DATA), true);
        check(attempts.get() == 2);
        check(Arrays.equals(DATA, Files.readAllBytes(target.toPath())));
        check(target.canExecute());
        // Source corruption: both attempts fail; the previous valid file survives.
        attempts.set(0);
        fails(() -> VerifiedExtraction.extract(() -> {
            attempts.incrementAndGet(); return new ByteArrayInputStream(DATA);
        }, target, DATA.length, crc(DATA) ^ 1, false));
        check(attempts.get() == 2);
        check(Arrays.equals(DATA, Files.readAllBytes(target.toPath())));
        fails(() -> VerifiedExtraction.extract(() -> new ByteArrayInputStream(DATA),
            target, DATA.length + 1, crc(DATA), false));
        fails(() -> VerifiedExtraction.extract(() -> new ByteArrayInputStream(DATA),
            target, DATA.length - 1, crc(DATA), false));
        fails(() -> VerifiedExtraction.extract(() -> new ByteArrayInputStream(DATA),
            target, -1, crc(DATA), false));
        // Interrupted staging is reclaimed, not accepted as a completed file.
        File staging = new File(root, ".mira-extract-library.so");
        Files.write(staging.toPath(), "partial".getBytes());
        VerifiedExtraction.extract(() -> new ByteArrayInputStream(DATA),
            target, DATA.length, crc(DATA), false);
        check(!staging.exists());
        // Detect same-length disk corruption with the read-back SHA-256 verifier.
        byte[] expectedHash = MessageDigest.getInstance("SHA-256").digest(DATA);
        byte[] damaged = DATA.clone(); damaged[0] ^= 1;
        Files.write(target.toPath(), damaged);
        fails(() -> VerifiedExtraction.verifyFile(target, DATA.length, expectedHash));
        File blocked = new File(root, "blocked"); check(blocked.mkdir());
        Files.write(new File(blocked, "user-file").toPath(), DATA);
        fails(() -> VerifiedExtraction.extract(() -> new ByteArrayInputStream(DATA),
            blocked, DATA.length, crc(DATA), false));
        check(new File(blocked, "user-file").isFile());
        // Empty Python package markers are legitimate (required runtime files are checked by caller).
        VerifiedExtraction.extract(() -> new ByteArrayInputStream(new byte[0]),
            target, 0, 0, false);
        check(target.length() == 0);
        // Exercise real ZIP streams for both packaging methods.
        for (int method : new int[] {ZipEntry.STORED, ZipEntry.DEFLATED}) {
            File zip = new File(root, "runtime-" + method + ".zip");
            try (ZipOutputStream output = new ZipOutputStream(new FileOutputStream(zip))) {
                ZipEntry entry = new ZipEntry("runtime.so"); entry.setMethod(method);
                entry.setSize(DATA.length); entry.setCrc(crc(DATA));
                output.putNextEntry(entry); output.write(DATA); output.closeEntry();
            }
            try (ZipFile input = new ZipFile(zip)) {
                ZipEntry entry = input.getEntry("runtime.so");
                VerifiedExtraction.extract(() -> input.getInputStream(entry), target,
                    entry.getSize(), entry.getCrc(), false);
                check(Arrays.equals(DATA, Files.readAllBytes(target.toPath())));
            }
        }
        System.out.println("PASS: retry, CRC, size, SHA-256 readback, interruption, replacement failure, STORED/DEFLATE");
    }
}
