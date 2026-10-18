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
fn identifiers(ts: TokenStream) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for t in ts {
        match t {
            proc_macro2::TokenTree::Ident(i) => {
                out.insert(i.to_string().trim_start_matches("r#").into());
            }
            proc_macro2::TokenTree::Group(g) => out.extend(identifiers(g.stream())),
            _ => (),
        }
    }
    out
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
    // Spec definitions reachable from the supplied model/contracts stay immutable.
    roots: BTreeSet<String>,
    specs: BTreeMap<String, (String, BTreeSet<String>)>,
    spec_spans: BTreeMap<String, Span>,
    proof_names: BTreeSet<String>,
    ranges: Vec<(usize, usize)>,
    lines: Vec<usize>,
    columns: Vec<Vec<usize>>,
    errors: Vec<String>,
    scope: Vec<String>,
}
impl Projection {
    fn new(source: &str, request: &Value) -> Self {
        let strings = |key: &str| {
            request[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_str().unwrap().to_owned())
                .collect()
        };
        let mut lines = vec![0];
        lines.extend(source.match_indices('\n').map(|(i, _)| i + 1));
        Self {
            protected: strings("protected"),
            contract_macros: strings("macros"),
            roots: strings("roots"),
            specs: BTreeMap::new(),
            spec_spans: BTreeMap::new(),
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
        let name = self
            .scope
            .iter()
            .chain(std::iter::once(&s.ident.to_string()))
            .cloned()
            .collect::<Vec<_>>()
            .join("::");
        let protected = public
            || self.protected.contains(&name)
            || self
                .protected
                .iter()
                .any(|p| p.rsplit("::").next() == Some(&s.ident.to_string()));
        if protected {
            let mut contract = s.spec.clone();
            contract.decreases = None;
            contract.prover = None;
            self.roots.extend(identifiers(contract.to_token_stream()));
        } else {
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
        let name = s.ident.to_string();
        self.proof_names
            .insert(format!("{}::{}", self.scope.join("::"), name));
        if spec_mode(s) {
            if public {
                self.roots.insert(name.clone());
            }
            let refs = identifiers(definition.clone());
            let key = format!("{}::{}", self.scope.join("::"), name);
            self.spec_spans.insert(key.clone(), span);
            if self
                .specs
                .insert(key, (definition.to_string(), refs))
                .is_some()
            {
                self.errors.push("ambiguous spec definition".into());
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
    fn visit_file_mut(&mut self, f: &mut File) {
        self.items(&mut f.items);
    }
    fn visit_item_mod_mut(&mut self, m: &mut ItemMod) {
        self.scope.push(m.ident.to_string());
        if let Some((_, items)) = &mut m.content {
            self.items(items);
        }
        self.scope.pop();
    }
    fn visit_item_impl_mut(&mut self, i: &mut ItemImpl) {
        // Preserve impl headers, including trait identities and generic bounds.
        self.scope.push(text(&i.self_ty));
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
    }
    fn visit_item_fn_mut(&mut self, f: &mut ItemFn) {
        for attr in &f.attrs {
            self.roots.extend(identifiers(attr.to_token_stream()));
        }
        self.signature(&mut f.sig, !matches!(f.vis, Visibility::Inherited));
        self.scope.push(f.sig.ident.to_string());
        self.visit_block_mut(&mut f.block);
        self.scope.pop();
    }
    fn visit_impl_item_fn_mut(&mut self, f: &mut ImplItemFn) {
        for attr in &f.attrs {
            self.roots.extend(identifiers(attr.to_token_stream()));
        }
        self.signature(&mut f.sig, !matches!(f.vis, Visibility::Inherited));
        self.scope.push(f.sig.ident.to_string());
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
                    self.record_proof(&f.sig, false, f.to_token_stream(), f.span());
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
    // Conservative name-based dependency closure protects the meaning of frozen contracts.
    let mut roots = pa.roots.clone();
    let mut dependencies = pa
        .specs
        .values()
        .map(|(_, refs)| refs.clone())
        .collect::<Vec<_>>();
    let mut dependency_names = pa
        .specs
        .keys()
        .map(|key| key.rsplit("::").next().unwrap().to_string())
        .collect::<Vec<_>>();
    let mut names = identifiers(before.parse().map_err(|_| "invalid frozen tokens")?);
    for source in request["context"].as_array().unwrap() {
        let source = source.as_str().unwrap();
        let mut f: File = syn::parse_str(source).map_err(|e| format!("context parse: {e}"))?;
        let mut p = Projection::new(source, request);
        p.visit_file_mut(&mut f);
        if !p.errors.is_empty() {
            return Err(format!("context parse: {:?}", p.errors));
        }
        roots.extend(p.roots);
        for (key, (_, refs)) in p.specs {
            dependency_names.push(key.rsplit("::").next().unwrap().to_string());
            dependencies.push(refs);
        }
        names.extend(identifiers(
            source.parse().map_err(|_| "invalid context tokens")?,
        ));
    }
    loop {
        let old = roots.len();
        for (name, refs) in dependency_names.iter().zip(&dependencies) {
            if roots.contains(name) {
                roots.extend(refs.clone());
            }
        }
        if roots.len() == old {
            break;
        }
    }
    for (key, (definition, _)) in &pa.specs {
        if roots.contains(key.rsplit("::").next().unwrap())
            && pb.specs.get(key).map(|x| &x.0) != Some(definition)
        {
            return Err(format!(
                "spec definition used by the authoritative contract changed: {key}"
            ));
        }
    }
    names.extend(
        request["roots"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap().to_string()),
    );
    for key in pb.proof_names.difference(&pa.proof_names) {
        let name = key.rsplit("::").next().unwrap();
        if !name.starts_with("audit_") || names.contains(name) {
            return Err(format!(
                "new proof/spec helpers must have fresh audit_ names: {name}"
            ));
        }
    }
    let frozen_regions = pb
        .spec_spans
        .iter()
        .filter(|(key, _)| roots.contains(key.rsplit("::").next().unwrap()))
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
