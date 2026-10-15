import Foundation

public struct OCRResult: Decodable, Sendable {
    public let text: String
    public let line_count: Int
    public let engine: String
    public let seconds: Double
}
public enum OCRError: Error { case invalidEndpoint, unavailable, imageTooLarge }

private final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public final class OCRClient: @unchecked Sendable {
    private let endpoint: URL
    private let token: String
    private let session: URLSession
    public init(endpoint: String, token: String, timeout: Double) throws {
        guard let url = URL(string: endpoint), url.scheme == "http", url.host == "127.0.0.1",
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { throw OCRError.invalidEndpoint }
        self.endpoint = url
        self.token = token
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }
    public func recognize(png: Data) async throws -> OCRResult {
        guard png.count <= 8 * 1024 * 1024 else { throw OCRError.imageTooLarge }
        var request = URLRequest(url: endpoint.appendingPathComponent("ocr"))
        request.httpMethod = "POST"
        request.setValue("image/png", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.httpBody = png
        let (body, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw OCRError.unavailable }
        return try JSONDecoder().decode(OCRResult.self, from: body)
    }
}
