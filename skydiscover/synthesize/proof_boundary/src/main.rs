//! A fail-closed correspondence check, not a verifier or a general Rust eraser.
//! Only parsed proof regions are erased. All other syntax must remain identical.
use proc_macro2::{Span, TokenStream};
use quote::{quote, ToTokens};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{self, Read},
};
use verus_syn::{
    self as syn,
    parse::{Parse, ParseStream},
    spanned::Spanned,
    visit_mut::{self, VisitMut},
    *,
};

fn text(t: &impl ToTokens) -> String {
    t.to_token_stream().to_string()
}
fn proof_mode(s: &Signature) -> bool {
    matches!(
        s.mode,
        FnMode::Spec(_) | FnMode::SpecChecked(_) | FnMode::Proof(_)
    )
}
fn spec_mode(s: &Signature) -> bool {
    matches!(s.mode, FnMode::Spec(_) | FnMode::SpecChecked(_))
}
// Canonicalize lookup keys only. Frozen syntax and definition text stay verbatim.
fn canonical_name(name: &str) -> &str {
    name.strip_prefix("r#").unwrap_or(name)
}
fn ident_name(ident: &Ident) -> String {
    canonical_name(&ident.to_string()).to_owned()
}
fn canonical_path(path: &str) -> String {
    path.split("::")
        .map(canonical_name)
        .collect::<Vec<_>>()
        .join("::")
}
fn identifiers(ts: TokenStream) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for t in ts {
        match t {
            proc_macro2::TokenTree::Ident(i) => {
                out.insert(ident_name(&i));
            }
            proc_macro2::TokenTree::Group(g) => out.extend(identifiers(g.stream())),
            _ => (),
        }
    }
    out
}

// The parser does not resolve names emitted by opaque macros. Even unchanged
// macro text can bind differently after adding a declaration. Inspect all tokens,
// including frozen spec bodies that Projection deliberately does not recurse into.
fn opaque_macro_names(ts: TokenStream) -> BTreeSet<String> {
    use proc_macro2::TokenTree;
    let tokens: Vec<_> = ts.into_iter().collect();
    let mut names = BTreeSet::new();
    for (index, token) in tokens.iter().enumerate() {
        if let TokenTree::Group(group) = token {
            names.extend(opaque_macro_names(group.stream()));
        }
        if let [TokenTree::Ident(name), TokenTree::Punct(bang), TokenTree::Group(_)] =
            &tokens[index..tokens.len().min(index + 3)]
        {
            if bang.as_char() == '!' {
                let name = ident_name(name);
                let qualified = index > 0
                    && matches!(&tokens[index - 1], TokenTree::Punct(p) if p.as_char() == ':');
                if qualified || !matches!(name.as_str(), "verus" | "verus_exec_expr") {
                    names.insert(name);
                }
            }
        }
    }
    names
}

// The frozen contract macro grammar already admitted by proof_source.macro_functions.
struct ContractBody {
    ty: Ident,
    methods: Vec<(Ident, TokenStream, Block)>,
}
impl Parse for ContractBody {
    fn parse(input: ParseStream) -> syn::Result<Self> {
        input.parse::<Token![impl]>()?;
        let ty = input.parse()?;
        let content;
        braced!(content in input);
        let mut methods = Vec::new();
        while !content.is_empty() {
            let name = content.parse()?;
            let args;
            parenthesized!(args in content);
            methods.push((name, args.parse()?, content.parse()?));
        }
        Ok(Self { ty, methods })
    }
}
impl ToTokens for ContractBody {
    fn to_tokens(&self, out: &mut TokenStream) {
        let ty = &self.ty;
        let methods = self
            .methods
            .iter()
            .map(|(name, args, body)| quote!(#name(#args) #body));
        out.extend(quote!(impl #ty { #(#methods)* }));
    }
}

struct Projection {
    protected: BTreeSet<String>,
    contract_macros: BTreeSet<String>,
    // Conservative dependency closure includes import aliases and re-exports.
    roots: BTreeSet<String>,
    dependencies: BTreeMap<String, BTreeSet<String>>,
    specs: BTreeMap<String, String>,
    spec_spans: BTreeMap<String, Span>,
    helper_signatures: BTreeMap<String, Signature>,
    proof_signatures: BTreeMap<String, Signature>,
    proof_names: BTreeSet<String>,
    ranges: Vec<(usize, usize)>,
    lines: Vec<usize>,
    columns: Vec<Vec<usize>>,
    errors: Vec<String>,
    scope: Vec<String>,
    impl_types: Vec<BTreeSet<String>>,
}
impl Projection {
    fn new(source: &str, request: &Value) -> Self {
        let strings = |key: &str| {
            request[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_str().unwrap().to_owned())
                .collect::<BTreeSet<String>>()
        };
        let mut lines = vec![0];
        lines.extend(source.match_indices('\n').map(|(i, _)| i + 1));
        Self {
            protected: strings("protected")
                .iter()
                .map(|s| canonical_path(s))
                .collect(),
            contract_macros: strings("macros"),
            roots: strings("roots")
                .iter()
                .flat_map(|s| {
                    canonical_path(s)
                        .split("::")
                        .map(str::to_owned)
                        .collect::<Vec<_>>()
                })
                .collect(),
            dependencies: BTreeMap::new(),
            specs: BTreeMap::new(),
            spec_spans: BTreeMap::new(),
            helper_signatures: BTreeMap::new(),
            proof_signatures: BTreeMap::new(),
            proof_names: BTreeSet::new(),
            ranges: Vec::new(),
            lines,
            columns: source
                .split('\n')
                .map(|line| {
                    line.char_indices()
                        .map(|(i, _)| i)
                        .chain(std::iter::once(line.len()))
                        .collect()
                })
                .collect(),
            errors: Vec::new(),
            scope: Vec::new(),
            impl_types: Vec::new(),
        }
    }
    fn offsets(&self, span: Span) -> (usize, usize) {
        let a = span.start();
        let b = span.end();
        (
            self.lines[a.line - 1] + self.columns[a.line - 1][a.column],
            self.lines[b.line - 1] + self.columns[b.line - 1][b.column],
        )
    }
    fn region(&mut self, span: Span) {
        self.ranges.push(self.offsets(span));
    }
    fn erase<T: ToTokens + Spanned>(&mut self, value: &mut Option<T>) {
        if let Some(v) = value {
            self.region(v.span());
        }
        *value = None;
    }
    fn signature(&mut self, s: &mut Signature, public: bool) {
        if let Some(protocol) = &s.spec.atomic_spec {
            self.roots.extend(identifiers(protocol.to_token_stream()));
        }
        let local_name = ident_name(&s.ident);
        let name = self
            .scope
            .iter()
            .chain(std::iter::once(&local_name))
            .cloned()
            .collect::<Vec<_>>()
            .join("::");
        // Targets are file-local module/type paths supplied by the adapter. A
        // matching basename in another scope does not make a helper authoritative.
        let protected = public || self.protected.contains(&name);
        if protected {
            self.roots
                .extend(identifiers(contract_signature(s).to_token_stream()));
        } else {
            // Internal contracts are checked at every caller and at the body by
            // Verus; they are proof obligations, not new public assumptions.
            if self.helper_signatures.insert(name, s.clone()).is_some() {
                self.errors.push("ambiguous helper definition".into());
            }
            self.erase(&mut s.spec.requires);
            self.erase(&mut s.spec.ensures);
            self.erase(&mut s.spec.recommends);
            self.erase(&mut s.spec.default_ensures);
            self.erase(&mut s.spec.returns);
            self.erase(&mut s.spec.invariants);
            // Atomic protocols, unwind guarantees and ghost calling conventions stay fixed.
            if let ReturnType::Type(_, _, name, _) = &mut s.output {
                *name = None;
            }
        }
        if s.spec.decreases.as_ref().is_some_and(|d| d.when.is_some()) {
            self.errors
                .push("conditional termination is outside the proof boundary".into());
        }
        self.erase(&mut s.spec.decreases);
        self.erase(&mut s.spec.prover);
    }
    fn record_proof(&mut self, s: &Signature, public: bool, definition: TokenStream, span: Span) {
        if s.spec.decreases.as_ref().is_some_and(|d| d.when.is_some()) {
            self.errors
                .push("conditional termination is outside the proof boundary".into());
        }
        let name = ident_name(&s.ident);
        if public
            || self
                .protected
                .iter()
                .any(|p| p.rsplit("::").next() == Some(name.as_str()))
        {
            self.roots.insert(name.clone());
            self.roots
                .extend(identifiers(contract_signature(s).to_token_stream()));
        }
        self.proof_names
            .insert(format!("{}::{}", self.scope.join("::"), name));
        if spec_mode(s) {
            self.dependencies
                .entry(name.clone())
                .or_default()
                .extend(identifiers(definition.clone()));
            // Type invariants are implicit contracts of any exposed containing
            // type. Private implementation types may evolve under Verus checks.
            if text(&definition).contains("verifier :: type_invariant") {
                if let Some(types) = self.impl_types.last() {
                    for ty in types {
                        self.dependencies
                            .entry(ty.clone())
                            .or_default()
                            .insert(name.clone());
                    }
                }
            }
            let key = format!("{}::{}", self.scope.join("::"), name);
            self.spec_spans.insert(key.clone(), span);
            if self.specs.insert(key, definition.to_string()).is_some() {
                self.errors.push("ambiguous spec definition".into());
            }
        }
        if matches!(s.mode, FnMode::Proof(_)) {
            let key = format!("{}::{}", self.scope.join("::"), name);
            if self.proof_signatures.insert(key, s.clone()).is_some() {
                self.errors.push("ambiguous proof definition".into());
            }
        }
        self.region(span);
    }
    fn items(&mut self, items: &mut Vec<Item>) {
        items.retain_mut(|item| {
            if let Item::Fn(f) = item {
                if proof_mode(&f.sig) {
                    self.record_proof(
                        &f.sig,
                        !matches!(f.vis, Visibility::Inherited),
                        f.to_token_stream(),
                        f.span(),
                    );
                    return false;
                }
            }
            self.visit_item_mut(item);
            true
        });
    }
}
impl VisitMut for Projection {
    fn visit_item_const_mut(&mut self, item: &mut ItemConst) {
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(ident_name(&item.ident));
        }
        self.dependencies
            .entry(ident_name(&item.ident))
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        visit_mut::visit_item_const_mut(self, item);
    }
    fn visit_item_static_mut(&mut self, item: &mut ItemStatic) {
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(ident_name(&item.ident));
        }
        self.dependencies
            .entry(ident_name(&item.ident))
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        visit_mut::visit_item_static_mut(self, item);
    }
    fn visit_impl_item_const_mut(&mut self, item: &mut ImplItemConst) {
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(ident_name(&item.ident));
        }
        self.dependencies
            .entry(ident_name(&item.ident))
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        visit_mut::visit_impl_item_const_mut(self, item);
    }
    fn visit_item_trait_mut(&mut self, item: &mut ItemTrait) {
        let name = ident_name(&item.ident);
        self.dependencies
            .entry(name.clone())
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(name);
        }
        visit_mut::visit_item_trait_mut(self, item);
    }
    fn visit_item_use_mut(&mut self, item: &mut ItemUse) {
        // Connect every identifier in an import in both directions. This safely
        // over-approximates grouped aliases, chains, module aliases and re-exports
        // without pretending to implement Rust's full name resolver.
        let names = identifiers(item.tree.to_token_stream());
        for name in &names {
            self.dependencies
                .entry(name.clone())
                .or_default()
                .extend(names.clone());
        }
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.extend(names);
        }
    }
    fn visit_item_struct_mut(&mut self, item: &mut ItemStruct) {
        let name = ident_name(&item.ident);
        self.dependencies
            .entry(name.clone())
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(name);
        }
        visit_mut::visit_item_struct_mut(self, item);
    }
    fn visit_item_enum_mut(&mut self, item: &mut ItemEnum) {
        let name = ident_name(&item.ident);
        self.dependencies
            .entry(name.clone())
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(name);
        }
        visit_mut::visit_item_enum_mut(self, item);
    }
    fn visit_item_type_mut(&mut self, item: &mut ItemType) {
        let name = ident_name(&item.ident);
        self.dependencies
            .entry(name.clone())
            .or_default()
            .extend(identifiers(item.to_token_stream()));
        if !matches!(item.vis, Visibility::Inherited) {
            self.roots.insert(name);
        }
        visit_mut::visit_item_type_mut(self, item);
    }
    fn visit_file_mut(&mut self, f: &mut File) {
        self.items(&mut f.items);
    }
    fn visit_item_mod_mut(&mut self, m: &mut ItemMod) {
        self.scope.push(ident_name(&m.ident));
        if let Some((_, items)) = &mut m.content {
            self.items(items);
        }
        self.scope.pop();
    }
    fn visit_item_impl_mut(&mut self, i: &mut ItemImpl) {
        // Preserve impl headers, including trait identities and generic bounds.
        // Generic arguments and raw identifier spelling do not change the type's
        // lookup path (e.g. Buffer<T>::read is configured as Buffer::read).
        let ty = match i.self_ty.as_ref() {
            Type::Path(p) if p.qself.is_none() => p
                .path
                .segments
                .iter()
                .map(|s| ident_name(&s.ident))
                .collect::<Vec<_>>()
                .join("::"),
            _ => text(&i.self_ty),
        };
        self.scope.push(ty);
        self.impl_types
            .push(identifiers(i.self_ty.to_token_stream()));
        i.items.retain_mut(|item| {
            if let ImplItem::Fn(f) = item {
                if proof_mode(&f.sig) {
                    self.record_proof(
                        &f.sig,
                        !matches!(f.vis, Visibility::Inherited),
                        f.to_token_stream(),
                        f.span(),
                    );
                    return false;
                }
            }
            self.visit_impl_item_mut(item);
            true
        });
        self.scope.pop();
        self.impl_types.pop();
    }
    fn visit_item_fn_mut(&mut self, f: &mut ItemFn) {
        for attr in &f.attrs {
            self.roots.extend(identifiers(attr.to_token_stream()));
        }
        self.signature(&mut f.sig, !matches!(f.vis, Visibility::Inherited));
        self.scope.push(ident_name(&f.sig.ident));
        self.visit_block_mut(&mut f.block);
        self.scope.pop();
    }
    fn visit_impl_item_fn_mut(&mut self, f: &mut ImplItemFn) {
        for attr in &f.attrs {
            self.roots.extend(identifiers(attr.to_token_stream()));
        }
        self.signature(&mut f.sig, !matches!(f.vis, Visibility::Inherited));
        self.scope.push(ident_name(&f.sig.ident));
        self.visit_block_mut(&mut f.block);
        self.scope.pop();
    }
    fn visit_block_mut(&mut self, b: &mut Block) {
        b.stmts.retain_mut(|s| {
            let erased = match s {
                Stmt::Local(l) => l.ghost.is_some() || l.tracked.is_some(),
                Stmt::Expr(e, _) => {
                    matches!(
                        e,
                        Expr::Assert(_) | Expr::AssertForall(_) | Expr::RevealHide(_)
                    ) || matches!(e, Expr::Unary(u) if matches!(u.op, UnOp::Proof(_)))
                }
                Stmt::Item(Item::Fn(f)) if proof_mode(&f.sig) => {
                    self.record_proof(
                        &f.sig,
                        !matches!(f.vis, Visibility::Inherited),
                        f.to_token_stream(),
                        f.span(),
                    );
                    return false;
                }
                _ => false,
            };
            if erased {
                self.region(s.span());
                false
            } else {
                self.visit_stmt_mut(s);
                true
            }
        });
    }
    fn visit_expr_while_mut(&mut self, e: &mut ExprWhile) {
        self.erase(&mut e.invariant);
        self.erase(&mut e.invariant_except_break);
        self.erase(&mut e.invariant_ensures);
        self.erase(&mut e.ensures);
        self.erase(&mut e.decreases);
        visit_mut::visit_expr_while_mut(self, e);
    }
    fn visit_expr_loop_mut(&mut self, e: &mut ExprLoop) {
        self.erase(&mut e.invariant);
        self.erase(&mut e.invariant_except_break);
        self.erase(&mut e.invariant_ensures);
        self.erase(&mut e.ensures);
        self.erase(&mut e.decreases);
        visit_mut::visit_expr_loop_mut(self, e);
    }
    fn visit_expr_for_loop_mut(&mut self, e: &mut ExprForLoop) {
        self.erase(&mut e.invariant);
        self.erase(&mut e.invariant_except_break);
        self.erase(&mut e.ensures);
        self.erase(&mut e.decreases);
        visit_mut::visit_expr_for_loop_mut(self, e);
    }
    fn visit_macro_mut(&mut self, m: &mut Macro) {
        let path = text(&m.path).replace(' ', "");
        let result: syn::Result<TokenStream> = if path == "verus" {
            syn::parse2::<File>(m.tokens.clone()).map(|mut f| {
                self.visit_file_mut(&mut f);
                f.into_token_stream()
            })
        } else if path == "verus_exec_expr" {
            syn::parse2::<Expr>(m.tokens.clone()).map(|mut e| {
                self.visit_expr_mut(&mut e);
                e.into_token_stream()
            })
        } else if self
            .contract_macros
            .iter()
            .any(|name| path == format!("crate::{name}"))
        {
            syn::parse2::<ContractBody>(m.tokens.clone()).map(|mut b| {
                for (_, _, body) in &mut b.methods {
                    self.visit_block_mut(body);
                }
                b.into_token_stream()
            })
        } else {
            return;
        }; // Opaque macros remain token-for-token frozen.
        match result {
            Ok(t) => m.tokens = t,
            Err(e) => self.errors.push(e.to_string()),
        }
    }
}
fn contract_signature(s: &Signature) -> Signature {
    let mut result = s.clone();
    result.spec.decreases = None;
    result.spec.prover = None;
    result
}
fn internal_signature(s: &Signature) -> Signature {
    let mut result = contract_signature(s);
    result.spec.requires = None;
    result.spec.ensures = None;
    result.spec.recommends = None;
    result.spec.default_ensures = None;
    result
}

fn check(request: &Value) -> std::result::Result<Value, String> {
    let before = request["before"].as_str().ok_or("missing before")?;
    let after = request["after"].as_str().ok_or("missing after")?;
    let mut a: File = syn::parse_str(before).map_err(|e| format!("frozen source parse: {e}"))?;
    let mut b: File = syn::parse_str(after).map_err(|e| format!("proof source parse: {e}"))?;
    let mut pa = Projection::new(before, request);
    pa.visit_file_mut(&mut a);
    let mut pb = Projection::new(after, request);
    pb.visit_file_mut(&mut b);
    if !pa.errors.is_empty() || !pb.errors.is_empty() {
        return Err(format!(
            "proof boundary parser: {:?} {:?}",
            pa.errors, pb.errors
        ));
    }
    if text(&a) != text(&b) {
        return Err("executable syntax, supplied/public contracts, imports, types, or opaque macros changed".into());
    }
    let mut roots = pa.roots.clone();
    let mut dependencies = pa.dependencies.clone();
    // Include newly attached type invariants, which can otherwise silently add
    // preconditions to a frozen public signature that mentions the owning type.
    for (name, refs) in &pb.dependencies {
        dependencies
            .entry(name.clone())
            .or_default()
            .extend(refs.clone());
    }
    let mut new_return_names = BTreeSet::new();
    for (key, original) in &pa.helper_signatures {
        let original = internal_signature(original);
        let mut candidate = internal_signature(
            pb.helper_signatures
                .get(key)
                .ok_or_else(|| format!("existing helper disappeared: {key}"))?,
        );
        if let ReturnType::Type(_, _, None, _) = &original.output {
            if let ReturnType::Type(_, _, binding, _) = &mut candidate.output {
                if let Some(binding) = binding.as_ref() {
                    // Conservatively reserve every identifier in a new return
                    // pattern, just as for newly declared proof helpers.
                    new_return_names.extend(identifiers(binding.1.to_token_stream()));
                }
                *binding = None;
            }
        }
        if text(&original) != text(&candidate) {
            return Err(format!("existing helper contract changed: {key}"));
        }
    }
    // All original/context identifiers are reserved for new helpers. This is
    // deliberately conservative: a fresh basename cannot shadow an old reference,
    // including an alias or a name brought in by a glob import. Imports and other
    // binding declarations outside erased proof regions remain syntax-frozen.
    let frozen_tokens: TokenStream = before.parse().map_err(|_| "invalid frozen tokens")?;
    let mut opaque_macros = opaque_macro_names(frozen_tokens.clone());
    let mut names = identifiers(frozen_tokens);
    for context in request["context"].as_array().unwrap() {
        // Each file has its own authoritative targets. Keep accepting strings for
        // callers with a single shared target scope.
        let mut context_request = request.clone();
        let source = if let Some(source) = context.as_str() {
            source
        } else {
            context_request["protected"] = context["protected"].clone();
            context["source"].as_str().ok_or("missing context source")?
        };
        let mut f: File = syn::parse_str(source).map_err(|e| format!("context parse: {e}"))?;
        let mut p = Projection::new(source, &context_request);
        p.visit_file_mut(&mut f);
        if !p.errors.is_empty() {
            return Err(format!("context parse: {:?}", p.errors));
        }
        roots.extend(p.roots);
        for (name, refs) in p.dependencies {
            dependencies.entry(name).or_default().extend(refs);
        }
        let context_tokens: TokenStream = source.parse().map_err(|_| "invalid context tokens")?;
        opaque_macros.extend(opaque_macro_names(context_tokens.clone()));
        names.extend(identifiers(context_tokens));
    }
    // Glob imports preserve basenames; any re-export renames are connected above.
    // Unknown expansions can conceal names entirely. Keep old definitions frozen
    // in that case rather than dropping an edge.
    if !opaque_macros.is_empty() {
        roots.extend(
            pa.specs
                .keys()
                .chain(pa.proof_signatures.keys())
                .map(|key| key.rsplit("::").next().unwrap().to_owned()),
        );
    }
    loop {
        let previous = roots.len();
        for (name, refs) in &dependencies {
            if roots.contains(name) {
                roots.extend(refs.clone());
            }
        }
        if previous == roots.len() {
            break;
        }
    }
    for (key, definition) in &pa.specs {
        if roots.contains(key.rsplit("::").next().unwrap()) && pb.specs.get(key) != Some(definition)
        {
            return Err(format!(
                "spec definition used by the authoritative contract changed: {key}"
            ));
        }
    }
    for (key, original) in &pa.proof_signatures {
        let candidate = pb
            .proof_signatures
            .get(key)
            .ok_or_else(|| format!("existing proof declaration disappeared: {key}"))?;
        let protected = roots.contains(key.rsplit("::").next().unwrap());
        let project = if protected {
            contract_signature
        } else {
            internal_signature
        };
        if text(&project(original)) != text(&project(candidate)) {
            return Err(format!("existing proof contract changed: {key}"));
        }
    }
    names.extend(request["roots"].as_array().unwrap().iter().flat_map(|s| {
        canonical_path(s.as_str().unwrap())
            .split("::")
            .map(str::to_owned)
            .collect::<Vec<_>>()
    }));
    for name in &new_return_names {
        if names.contains(name) {
            return Err(format!(
                "new return binding must not shadow a frozen name: {name}"
            ));
        }
    }
    for key in pb.proof_names.difference(&pa.proof_names) {
        let name = key.rsplit("::").next().unwrap();
        if !name.starts_with("audit_") || names.contains(name) {
            return Err(format!(
                "new proof/spec helpers must have fresh audit_ names: {name}"
            ));
        }
    }
    for key in pb.specs.keys().filter(|key| !pa.specs.contains_key(*key)) {
        if roots.contains(key.rsplit("::").next().unwrap()) {
            return Err(format!(
                "new spec changes an authoritative or implicit type contract: {key}"
            ));
        }
    }
    if (!new_return_names.is_empty() || pb.proof_names.difference(&pa.proof_names).next().is_some())
        && !opaque_macros.is_empty()
    {
        return Err(format!(
            "cannot establish new name bindings across opaque macros: {:?}",
            opaque_macros
        ));
    }
    let frozen_regions = pb
        .spec_spans
        .iter()
        .filter(|(key, _)| {
            pa.specs.contains_key(*key) && roots.contains(key.rsplit("::").next().unwrap())
        })
        .map(|(_, span)| pb.offsets(*span))
        .collect::<BTreeSet<_>>();
    // Do not even rebind macro calls inside a protected definition: its meaning is frozen.
    pb.ranges.retain(|range| !frozen_regions.contains(range));
    pb.ranges.sort();
    Ok(json!({"ranges": pb.ranges}))
}
fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let result = serde_json::from_str::<Value>(&input)
        .map_err(|e| e.to_string())
        .and_then(|r| check(&r));
    match result {
        Ok(v) => println!("{}", json!({"ok": true, "ranges": v["ranges"]})),
        Err(e) => println!("{}", json!({"ok": false, "error": e})),
    }
}
