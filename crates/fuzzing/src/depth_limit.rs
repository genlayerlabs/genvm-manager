use core::fmt;
use serde::de::{self, DeserializeSeed, Visitor};

pub(crate) struct Deserializer<D> {
    inner: D,
    remaining: usize,
}

impl<D> Deserializer<D> {
    pub(crate) fn new(inner: D, remaining: usize) -> Self {
        Self { inner, remaining }
    }

    fn descend<'de, V>(self, visitor: V) -> Result<(D, LimitVisitor<V>), D::Error>
    where
        D: de::Deserializer<'de>,
    {
        let remaining = self
            .remaining
            .checked_sub(1)
            .ok_or_else(|| de::Error::custom("deserialization depth limit exceeded"))?;
        Ok((
            self.inner,
            LimitVisitor {
                inner: visitor,
                remaining,
            },
        ))
    }
}

macro_rules! delegate {
    ($($method:ident $(($($arg:ident: $ty:ty),*))?);+ $(;)?) => {
        $(
            fn $method<V>(self, $($($arg: $ty,)*)? visitor: V) -> Result<V::Value, Self::Error>
            where
                V: Visitor<'de>,
            {
                self.inner.$method($($($arg,)*)? visitor)
            }
        )+
    };
}

impl<'de, D> de::Deserializer<'de> for Deserializer<D>
where
    D: de::Deserializer<'de>,
{
    type Error = D::Error;

    delegate! {
        deserialize_any();
        deserialize_bool();
        deserialize_i8();
        deserialize_i16();
        deserialize_i32();
        deserialize_i64();
        deserialize_i128();
        deserialize_u8();
        deserialize_u16();
        deserialize_u32();
        deserialize_u64();
        deserialize_u128();
        deserialize_f32();
        deserialize_f64();
        deserialize_char();
        deserialize_str();
        deserialize_string();
        deserialize_bytes();
        deserialize_byte_buf();
        deserialize_unit();
        deserialize_unit_struct(name: &'static str);
        deserialize_identifier();
        deserialize_ignored_any();
    }

    fn deserialize_option<V>(self, visitor: V) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_option(visitor)
    }

    fn deserialize_newtype_struct<V>(
        self,
        name: &'static str,
        visitor: V,
    ) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_newtype_struct(name, visitor)
    }

    fn deserialize_seq<V>(self, visitor: V) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_seq(visitor)
    }

    fn deserialize_tuple<V>(self, len: usize, visitor: V) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_tuple(len, visitor)
    }

    fn deserialize_tuple_struct<V>(
        self,
        name: &'static str,
        len: usize,
        visitor: V,
    ) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_tuple_struct(name, len, visitor)
    }

    fn deserialize_map<V>(self, visitor: V) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_map(visitor)
    }

    fn deserialize_struct<V>(
        self,
        name: &'static str,
        fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_struct(name, fields, visitor)
    }

    fn deserialize_enum<V>(
        self,
        name: &'static str,
        variants: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let (inner, visitor) = self.descend(visitor)?;
        inner.deserialize_enum(name, variants, visitor)
    }

    fn is_human_readable(&self) -> bool {
        self.inner.is_human_readable()
    }
}

struct LimitVisitor<V> {
    inner: V,
    remaining: usize,
}

impl<'de, V> Visitor<'de> for LimitVisitor<V>
where
    V: Visitor<'de>,
{
    type Value = V::Value;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.inner.expecting(formatter)
    }

    fn visit_none<E>(self) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        self.inner.visit_none()
    }

    fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        self.inner
            .visit_some(Deserializer::new(deserializer, self.remaining))
    }

    fn visit_newtype_struct<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        self.inner
            .visit_newtype_struct(Deserializer::new(deserializer, self.remaining))
    }

    fn visit_seq<A>(self, seq: A) -> Result<Self::Value, A::Error>
    where
        A: de::SeqAccess<'de>,
    {
        self.inner.visit_seq(LimitSeqAccess {
            inner: seq,
            remaining: self.remaining,
        })
    }

    fn visit_map<A>(self, map: A) -> Result<Self::Value, A::Error>
    where
        A: de::MapAccess<'de>,
    {
        self.inner.visit_map(LimitMapAccess {
            inner: map,
            remaining: self.remaining,
        })
    }

    fn visit_enum<A>(self, data: A) -> Result<Self::Value, A::Error>
    where
        A: de::EnumAccess<'de>,
    {
        self.inner.visit_enum(LimitEnumAccess {
            inner: data,
            remaining: self.remaining,
        })
    }
}

struct LimitSeed<S> {
    inner: S,
    remaining: usize,
}

impl<'de, S> DeserializeSeed<'de> for LimitSeed<S>
where
    S: DeserializeSeed<'de>,
{
    type Value = S::Value;

    fn deserialize<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        self.inner
            .deserialize(Deserializer::new(deserializer, self.remaining))
    }
}

struct LimitSeqAccess<A> {
    inner: A,
    remaining: usize,
}

impl<'de, A> de::SeqAccess<'de> for LimitSeqAccess<A>
where
    A: de::SeqAccess<'de>,
{
    type Error = A::Error;

    fn next_element_seed<T>(&mut self, seed: T) -> Result<Option<T::Value>, Self::Error>
    where
        T: DeserializeSeed<'de>,
    {
        self.inner.next_element_seed(LimitSeed {
            inner: seed,
            remaining: self.remaining,
        })
    }

    fn size_hint(&self) -> Option<usize> {
        self.inner.size_hint()
    }
}

struct LimitMapAccess<A> {
    inner: A,
    remaining: usize,
}

impl<'de, A> de::MapAccess<'de> for LimitMapAccess<A>
where
    A: de::MapAccess<'de>,
{
    type Error = A::Error;

    fn next_key_seed<K>(&mut self, seed: K) -> Result<Option<K::Value>, Self::Error>
    where
        K: DeserializeSeed<'de>,
    {
        self.inner.next_key_seed(LimitSeed {
            inner: seed,
            remaining: self.remaining,
        })
    }

    fn next_value_seed<V>(&mut self, seed: V) -> Result<V::Value, Self::Error>
    where
        V: DeserializeSeed<'de>,
    {
        self.inner.next_value_seed(LimitSeed {
            inner: seed,
            remaining: self.remaining,
        })
    }

    fn size_hint(&self) -> Option<usize> {
        self.inner.size_hint()
    }
}

struct LimitEnumAccess<A> {
    inner: A,
    remaining: usize,
}

impl<'de, A> de::EnumAccess<'de> for LimitEnumAccess<A>
where
    A: de::EnumAccess<'de>,
{
    type Error = A::Error;
    type Variant = LimitVariantAccess<A::Variant>;

    fn variant_seed<V>(self, seed: V) -> Result<(V::Value, Self::Variant), Self::Error>
    where
        V: DeserializeSeed<'de>,
    {
        let (value, variant) = self.inner.variant_seed(LimitSeed {
            inner: seed,
            remaining: self.remaining,
        })?;
        Ok((
            value,
            LimitVariantAccess {
                inner: variant,
                remaining: self.remaining,
            },
        ))
    }
}

struct LimitVariantAccess<A> {
    inner: A,
    remaining: usize,
}

impl<'de, A> de::VariantAccess<'de> for LimitVariantAccess<A>
where
    A: de::VariantAccess<'de>,
{
    type Error = A::Error;

    fn unit_variant(self) -> Result<(), Self::Error> {
        self.inner.unit_variant()
    }

    fn newtype_variant_seed<T>(self, seed: T) -> Result<T::Value, Self::Error>
    where
        T: DeserializeSeed<'de>,
    {
        self.inner.newtype_variant_seed(LimitSeed {
            inner: seed,
            remaining: self.remaining,
        })
    }

    fn tuple_variant<V>(self, len: usize, visitor: V) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let remaining = self
            .remaining
            .checked_sub(1)
            .ok_or_else(|| de::Error::custom("deserialization depth limit exceeded"))?;
        self.inner.tuple_variant(
            len,
            LimitVisitor {
                inner: visitor,
                remaining,
            },
        )
    }

    fn struct_variant<V>(
        self,
        fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Self::Error>
    where
        V: Visitor<'de>,
    {
        let remaining = self
            .remaining
            .checked_sub(1)
            .ok_or_else(|| de::Error::custom("deserialization depth limit exceeded"))?;
        self.inner.struct_variant(
            fields,
            LimitVisitor {
                inner: visitor,
                remaining,
            },
        )
    }
}
