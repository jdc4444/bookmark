// OCR a batch of image files using Apple's Vision framework.
//
// Usage: ./ocr <image_path> [<image_path> ...]
// Output (stdout): one JSON object per line: {"path": "...", "ok": true, "text": "..."}
//                  or                       {"path": "...", "ok": false, "error": "..."}

import Foundation
import Vision
import AppKit

func ocr(path: String) -> [String: Any] {
    guard let image = NSImage(contentsOfFile: path) else {
        return ["path": path, "ok": false, "error": "could_not_load_image"]
    }
    var rect = NSRect(x: 0, y: 0, width: image.size.width, height: image.size.height)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        return ["path": path, "ok": false, "error": "could_not_convert_to_cgimage"]
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return ["path": path, "ok": false, "error": "vision_perform_failed: \(error.localizedDescription)"]
    }

    let observations = request.results ?? []
    var lines: [String] = []
    var blocks: [[String: Any]] = []
    for obs in observations {
        guard let candidate = obs.topCandidates(1).first else { continue }
        let text = candidate.string
        if text.isEmpty { continue }
        lines.append(text)
        // Vision returns boundingBox in normalized image coords with origin
        // at bottom-left. Convert to top-left origin (HTML coords).
        let bb = obs.boundingBox
        let x = Double(bb.origin.x)
        let y = 1.0 - Double(bb.origin.y) - Double(bb.size.height)
        let w = Double(bb.size.width)
        let h = Double(bb.size.height)
        blocks.append([
            "text": text,
            "x": x, "y": y, "w": w, "h": h,
            "confidence": Double(candidate.confidence),
        ])
    }
    let text = lines.joined(separator: "\n")
    return [
        "path": path, "ok": true,
        "text": text,
        "lines": lines.count,
        "blocks": blocks,
    ]
}

let args = CommandLine.arguments.dropFirst()
if args.isEmpty {
    FileHandle.standardError.write("usage: ocr <image_path> [...]\n".data(using: .utf8)!)
    exit(2)
}

let queue = DispatchQueue(label: "ocr-stdout")
let group = DispatchGroup()
let workQueue = DispatchQueue.global(qos: .userInitiated)

for arg in args {
    group.enter()
    workQueue.async {
        let result = ocr(path: arg)
        if let data = try? JSONSerialization.data(withJSONObject: result, options: []),
           let line = String(data: data, encoding: .utf8) {
            queue.sync {
                FileHandle.standardOutput.write(line.data(using: .utf8)!)
                FileHandle.standardOutput.write("\n".data(using: .utf8)!)
            }
        }
        group.leave()
    }
}
group.wait()
