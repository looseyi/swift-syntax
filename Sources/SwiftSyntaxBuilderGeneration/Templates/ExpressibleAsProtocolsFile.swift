//===----------------------------------------------------------------------===//
//
// This source file is part of the Swift.org open source project
//
// Copyright (c) 2014 - 2022 Apple Inc. and the Swift project authors
// Licensed under Apache License v2.0 with Runtime Library Exception
//
// See https://swift.org/LICENSE.txt for license information
// See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
//
//===----------------------------------------------------------------------===//

import Foundation
import SwiftSyntax
import SwiftSyntaxBuilder

let expressibleAsProtocolsFile = SourceFile {
  ImportDecl(
    leadingTrivia: .docLineComment(copyrightHeader),
    path: "SwiftSyntax"
  )

  // See BuildableBaseProtocols why `SyntaxCollection` is excluded here.
  let expressibleAsTypes =
    SYNTAX_BASE_KINDS
      .filter { $0 != "SyntaxCollection" }
      .sorted() // Make sure that the order is deterministic
      .map { SyntaxBuildableType(syntaxKind: $0) }
    + SYNTAX_NODES
      .filter { $0.isBuildable || $0.isSyntaxCollection }
      .map { $0.type }
  
  for type in expressibleAsTypes {
    let baseType = type.baseType
    let conformances = type.generatedExpressibleAsConformances
    // Exclude conformances that are already implied because otherwise Swift throws a warning.
    let impliedConformances = Set(type.impliedExpressibleAsConformances)
    let declaredConformances = conformances.filter { !impliedConformances.contains($0) }

    ProtocolDecl(
      modifiers: [TokenSyntax.public],
      identifier: type.expressibleAs,
      inheritanceClause: createTypeInheritanceClause(conformances: declaredConformances.map(\.expressibleAs))
    ) {
      FunctionDecl(
        identifier: .identifier("create\(type.buildableBaseName)"),
        signature: FunctionSignature(
          input: ParameterClause(),
          output: type.buildable
        ),
        body: nil
      )
    }

    if !conformances.isEmpty {
      ExtensionDecl(
        modifiers: [TokenSyntax.public],
        extendedType: type.expressibleAs
      ) {
        for conformance in type.elementInCollections {
          FunctionDecl(
            leadingTrivia: .docLineComment("/// Conformance to `\(conformance.expressibleAs)`") + .newline,
            identifier: .identifier("create\(conformance.buildableBaseName)"),
            signature: FunctionSignature(
              input: ParameterClause(),
              output: conformance.buildable
            )
          ) {
            ReturnStmt(expression: FunctionCallExpr(conformance.buildable) {
              TupleExprElement(expression: ArrayExpr {
                ArrayElement(expression: "self")
              })
            })
          }
        }
        for conformance in type.convertibleToTypes {
          let param = Node.from(type: conformance).singleNonDefaultedChild
          FunctionDecl(
            leadingTrivia: .docLineComment("/// Conformance to \(conformance.expressibleAs)") + .newline,
            identifier: .identifier("create\(conformance.buildableBaseName)"),
            signature: FunctionSignature(
              input: ParameterClause(),
              output: conformance.buildable
            )
          ) {
            ReturnStmt(expression: FunctionCallExpr(conformance.buildable) {
              TupleExprElement(label: param.swiftName, expression: "self")
            })
          }
        }
        if let baseType = baseType, baseType.baseName != "SyntaxCollection" {
          FunctionDecl(
            identifier: .identifier("create\(baseType.buildableBaseName)"),
            signature: FunctionSignature(
              input: ParameterClause(),
              output: baseType.buildable
            )
          ) {
            ReturnStmt(expression: FunctionCallExpr("create\(type.buildableBaseName)"))
          }
        }
      }
    }
  }
}
