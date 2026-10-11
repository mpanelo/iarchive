use std::collections::BTreeMap;

/// BK-tree over 64-bit hashes. Equal hashes share a node instead of forming a deep chain.
#[derive(Default)]
pub struct HashTree {
    root: Option<Box<Node>>,
}
struct Node {
    hash: u64,
    members: Vec<usize>,
    children: BTreeMap<u32, Box<Node>>,
}
impl HashTree {
    pub fn insert(&mut self, hash: u64, index: usize) {
        let Some(mut node) = self.root.as_mut() else {
            self.root = Some(Box::new(Node {
                hash,
                members: vec![index],
                children: BTreeMap::new(),
            }));
            return;
        };
        loop {
            let distance = (hash ^ node.hash).count_ones();
            if distance == 0 {
                node.members.push(index);
                return;
            }
            match node.children.entry(distance) {
                std::collections::btree_map::Entry::Occupied(entry) => node = entry.into_mut(),
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(Box::new(Node {
                        hash,
                        members: vec![index],
                        children: BTreeMap::new(),
                    }));
                    return;
                }
            }
        }
    }
    pub fn query(&self, hash: u64, radius: u32) -> Vec<usize> {
        self.query_limited(hash, radius, usize::MAX)
    }
    pub fn query_limited(&self, hash: u64, radius: u32, limit: usize) -> Vec<usize> {
        let mut found = Vec::new();
        let mut stack = Vec::new();
        if let Some(root) = &self.root {
            stack.push(root.as_ref());
        }
        while let Some(node) = stack.pop() {
            let distance = (hash ^ node.hash).count_ones();
            if distance <= radius {
                found.extend(node.members.iter().take(limit.saturating_sub(found.len())));
                if found.len() >= limit {
                    break;
                }
            }
            for (_, child) in node
                .children
                .range(distance.saturating_sub(radius)..=distance.saturating_add(radius))
            {
                stack.push(child.as_ref());
            }
        }
        found
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn candidate_count_is_bounded() {
        let mut tree = HashTree::default();
        for i in 0..10000 {
            tree.insert(0, i);
        }
        assert_eq!(tree.query_limited(0, 6, 64).len(), 64);
    }
    #[test]
    fn bk_tree_matches_exhaustive_search() {
        let hashes = [
            0,
            1,
            3,
            7,
            0xffff,
            0xdeadbeef,
            0x12345678,
            0xffffffffffffffff,
            0,
        ];
        let mut tree = HashTree::default();
        for (i, hash) in hashes.iter().enumerate() {
            tree.insert(*hash, i);
        }
        for hash in hashes {
            let mut actual = tree.query(hash, 6);
            actual.sort_unstable();
            let expected: Vec<_> = hashes
                .iter()
                .enumerate()
                .filter(|(_, v)| (**v ^ hash).count_ones() <= 6)
                .map(|(i, _)| i)
                .collect();
            assert_eq!(actual, expected);
        }
    }
}
