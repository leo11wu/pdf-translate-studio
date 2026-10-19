import AppKit

let destination = CommandLine.arguments[1]
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                              samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                              bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor(calibratedRed: 0.086, green: 0.243, blue: 0.188, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 200, yRadius: 200).fill()
NSColor(calibratedRed: 0.96, green: 0.95, blue: 0.92, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 260, y: 200, width: 505, height: 635), xRadius: 50, yRadius: 50).fill()
NSColor(calibratedRed: 0.91, green: 0.42, blue: 0.28, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 200, y: 280, width: 400, height: 400), xRadius: 75, yRadius: 75).fill()
let text = "译" as NSString
let attributes: [NSAttributedString.Key: Any] = [
    .font: NSFont(name: "PingFangSC-Semibold", size: 272) ?? NSFont.systemFont(ofSize: 272, weight: .semibold),
    .foregroundColor: NSColor.white,
]
text.draw(at: NSPoint(x: 264, y: 303), withAttributes: attributes)
NSColor(calibratedRed: 0.18, green: 0.44, blue: 0.34, alpha: 0.45).setFill()
for y in [720, 670] { NSBezierPath(roundedRect: NSRect(x: 628, y: y, width: 76, height: 13), xRadius: 6, yRadius: 6).fill() }
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: destination))
