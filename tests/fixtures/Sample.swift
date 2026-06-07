import Foundation

final class Caller {
    func doWork(x: String, y: [String]) {
        // (1) static type-qualified call -> should resolve to ProjectionSvc.reconcile
        try? ProjectionSvc.reconcile(id: x)

        // (2) singleton-qualified call -> should resolve to ProjectionSvc.fetch
        let v = ProjectionSvc.shared.fetch(q: y)
        _ = v

        // (3) unqualified call -> skipped (needs type inference, out of scope)
        helper()

        // (4) receiver is not a graph node -> skipped (unknown receiver)
        Unknown.method()

        // (5) receiver label maps to >1 class node -> skipped (ambiguous, precision guard)
        Ambig.foo()

        // (6) receiver known but class does not own this method -> skipped
        ProjectionSvc.missingMethod()
    }

    func helper() {}
}
