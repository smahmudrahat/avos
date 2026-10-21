from slugify import slugify

assert slugify("Hello World") == "hello-world"
assert slugify("  Hello,   World!  ") == "hello-world", slugify("  Hello,   World!  ")
assert slugify("C++ & Rust: 2026 edition") == "c-rust-2026-edition", slugify("C++ & Rust: 2026 edition")
assert slugify("---") == ""
print("ok")
